import argparse
import os

import numpy as np
import pandas as pd

# Two RPT definitions are reported for EVERY mode, so URR and CER keep their own meaning:
#   rpt_index_s : time until the INDEX stops returning the protected chunk   (goes with URR)
#   rpt_ctx_s   : time until the chunk stops reaching the LLM CONTEXT        (goes with CER)
# For periodic/event there is no gate, so the two coincide. For query-time/hybrid the index
# stays stale on purpose, so rpt_index is censored while rpt_ctx is ~0.
# URR-side interval measures end at index enforcement; CER-side ones end at context enforcement.
#
# Censored trials (never stably blocked) are NOT dropped from the RPT statistics: km_median
# and km_p95 are Kaplan-Meier estimates that use "still not blocked after obs_s seconds".
# The plain ctx_mean / ctx_median / ctx_p95 / ctx_max cover blocked trials only.

MIN_REL_PROBES = 30      # fewer relevant probes than this inside the interval -> rate too noisy


def stable_block_time(g, k, col="protected_returned"):
    """First attempt after which k consecutive attempts are all blocked
    (and it stays blocked for the rest of the trial). Returns timestamp or NaN."""
    blocked = (g[col] == 0).to_numpy()
    ts = g["attempt_ts"].to_numpy()
    n = len(g)
    all_blocked_from = np.zeros(n, dtype=bool)
    run = True
    for i in range(n - 1, -1, -1):
        run = run and blocked[i]
        all_blocked_from[i] = run
    for i in range(n - k + 1):
        if all_blocked_from[i]:
            return ts[i]
    return np.nan


def km_quantile(durations, observed, p):
    """Kaplan-Meier: time by which a share p of revocations is enforced, using censored
    trials as 'not yet enforced after their observation time'. inf = not reached."""
    d = np.asarray(durations, float)
    e = np.asarray(observed, bool)
    if len(d) == 0:
        return np.nan
    surv = 1.0
    for t in np.unique(d[e]):
        at_risk = (d >= t).sum()
        surv *= 1 - ((d == t) & e).sum() / at_risk
        if 1 - surv >= p - 1e-12:
            return float(t)
    return np.inf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", required=True)
    ap.add_argument("--attempts", required=True)
    ap.add_argument("--out", default="clean")
    ap.add_argument("--k", type=int, default=3, help="consecutive blocked attempts = enforced")
    ap.add_argument("--warmup", type=int, default=3, help="trials to drop per mode+config")
    ap.add_argument("--window", type=float, default=0,
                    help="cap (s) for the whole-window urr/cer columns; 0 = use every attempt")
    ap.add_argument("--max-error-rate", type=float, default=0.05)
    ap.add_argument("--qpm", type=float, default=1.0,
                    help="ASSUMED queries per minute from the revoked user (for expected exposures)")
    ap.add_argument("--share-relevant", type=float, default=0.1,
                    help="ASSUMED share of those queries that are about the revoked document")
    ap.add_argument("--stale-rate", type=float, default=None,
                    help="override the stale-state context exposure rate used when a mode's own "
                         "interval is too short to measure (default: pooled from periodic / no-worker)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    trials = pd.read_csv(args.trials)
    att = pd.read_csv(args.attempts)
    excl = []

    def drop(ids, reason):
        for t in ids:
            excl.append({"trial_id": t, "reason": reason})

    # 1. basic integrity. A duplicated trial_id makes its attempts ambiguous: drop EVERY copy
    dup = trials.loc[trials.duplicated("trial_id", keep=False), "trial_id"].unique()
    drop(dup, "duplicate trial_id (attempts ambiguous)")
    trials = trials[~trials.trial_id.isin(dup)]
    att = att[~att.trial_id.isin(dup)].drop_duplicates()
    bad = trials[trials[["mode", "config", "t_revocation"]].isna().any(axis=1)]["trial_id"]
    drop(bad, "missing required field")
    trials = trials[~trials.trial_id.isin(bad)]

    # 2. trials that failed before measuring anything useful
    f = trials[trials["baseline_ok"] != 1]["trial_id"]
    drop(f, "baseline check failed (user could not retrieve protected doc)")
    trials = trials[~trials.trial_id.isin(f)]
    f = trials[trials["status"] == "crashed"]["trial_id"]
    drop(f, "trial interrupted (crash or Ctrl+C); ACL was restored")
    trials = trials[~trials.trial_id.isin(f)]
    f = trials[trials["status"] != "ok"]["trial_id"]
    drop(f, "status not ok")
    trials = trials[~trials.trial_id.isin(f)]

    # 3. warm-up: first N trials per mode+config (ordered by revocation time)
    trials = trials.sort_values("t_revocation")
    warm = trials[trials["config"] != "no-worker"].groupby(["mode", "config"]).head(args.warmup)["trial_id"]
    drop(warm, "warm-up trial")
    trials = trials[~trials.trial_id.isin(warm)]

    # 4. attempts: keep only post-revocation attempts of surviving trials
    att = att.merge(trials[["trial_id", "t_revocation"]], on="trial_id", how="inner")
    att = att[att["attempt_ts"] >= att["t_revocation"]]
    att = att.sort_values(["trial_id", "attempt_ts"])

    # RPT comes from the clean REFERENCE search when the logs have it (new format);
    # URR and CER always come from the WORKLOAD search. Old logs fall back to the old columns.
    idx_col = "ref_returned" if "ref_returned" in att.columns else "protected_returned"
    ctx_col = "ref_in_prompt" if "ref_in_prompt" in att.columns else "protected_in_prompt"

    rows = []
    for tid, g in att.groupby("trial_id"):
        t_rev = g["t_revocation"].iloc[0]
        err = (g["http_status"] != 200).mean()
        if len(g) < args.k:
            drop([tid], "too few attempts"); continue
        if err > args.max_error_rate:
            drop([tid], f"error rate {err:.1%} > {args.max_error_rate:.0%}"); continue
        g = g[g["http_status"] == 200]                              # errors are not 'blocked'

        t_idx = stable_block_time(g, args.k, idx_col)
        t_ctx = stable_block_time(g, args.k, ctx_col)
        rpt_idx = np.nan if np.isnan(t_idx) else t_idx - t_rev
        rpt_ctx = np.nan if np.isnan(t_ctx) else t_ctx - t_rev
        obs_s = g["attempt_ts"].max() - t_rev                       # how long the trial was watched

        w = g[g["attempt_ts"] <= t_rev + args.window] if args.window > 0 else g
        has_rel = "relevant" in g.columns

        def interval(t_stop, col):
            """Probes from revocation until t_stop (or the last attempt): all of them, and only
            the ones about the protected document. Returns the probes, then (n, hits, n_rel, hits_rel)."""
            o = g[g["attempt_ts"] < t_stop]
            r = o[o["relevant"] == 1] if has_rel else o
            return o, len(o), int(o[col].sum()), len(r), int(r[col].sum())

        o_idx, n_idx, k_urr, nr_idx, kr_urr = interval(np.inf if np.isnan(t_idx) else t_idx, "protected_returned")
        o_ctx, n_ctx, k_cer, nr_ctx, kr_cer = interval(np.inf if np.isnan(t_ctx) else t_ctx, "protected_in_prompt")
        div = lambda a_, b_: a_ / b_ if b_ else np.nan
        rows.append({
            "trial_id": tid,
            "rpt_index_s": rpt_idx, "censored_index": bool(np.isnan(rpt_idx)),
            "rpt_ctx_s": rpt_ctx, "censored_ctx": bool(np.isnan(rpt_ctx)),
            "obs_s": obs_s,
            "urr": w["protected_returned"].mean() if len(w) else np.nan,
            "cer": w["protected_in_prompt"].mean() if len(w) else np.nan,
            "urr_interval": div(k_urr, n_idx), "cer_interval": div(k_cer, n_ctx),
            "urr_rel_interval": div(kr_urr, nr_idx), "cer_rel_interval": div(kr_cer, nr_ctx),
            "leaked_probes": k_urr,
            "n_idx": n_idx, "k_urr": k_urr, "nr_idx": nr_idx, "kr_urr": kr_urr,
            "n_ctx": n_ctx, "k_cer": k_cer, "nr_ctx": nr_ctx, "kr_cer": kr_cer,
            "sum_chunks": float(o_ctx["ctx_chunks"].sum()) if "ctx_chunks" in o_ctx.columns else np.nan,
            "sum_chars": float(o_ctx["ctx_chars"].sum()) if "ctx_chars" in o_ctx.columns else np.nan,
            "lat_mean": g["latency_ms"].mean(),
            "lat_p95": g["latency_ms"].quantile(0.95),
            "n_attempts": len(g), "error_rate": err,
        })

    if not rows:
        print("no trials survived cleaning"); return
    res = pd.DataFrame(rows).merge(trials[["trial_id", "mode", "config"]], on="trial_id")
    missing = set(trials.trial_id) - set(res.trial_id) - {e["trial_id"] for e in excl}
    drop(missing, "no valid post-revocation attempts")

    res.to_csv(f"{args.out}/clean_trials.csv", index=False)
    pd.DataFrame(excl, columns=["trial_id", "reason"]).to_csv(f"{args.out}/exclusions.csv", index=False)

    # 5. summary. Outliers are KEPT (tail values are the finding); censored are counted and
    # enter the Kaplan-Meier columns. No p99: with ~30 trials it is just the maximum.
    def q(p): return lambda s: s.quantile(p)
    def tot(s): return s.sum(min_count=1)
    summ = res.groupby(["mode", "config"]).agg(
        trials=("trial_id", "count"),
        cens_idx=("censored_index", "sum"), cens_ctx=("censored_ctx", "sum"),
        idx_mean=("rpt_index_s", "mean"), idx_p95=("rpt_index_s", q(0.95)),
        ctx_mean=("rpt_ctx_s", "mean"), ctx_median=("rpt_ctx_s", "median"),
        ctx_p95=("rpt_ctx_s", q(0.95)), ctx_max=("rpt_ctx_s", "max"),
        obs_mean=("obs_s", "mean"),
        urr_mean=("urr", "mean"), cer_mean=("cer", "mean"),
        lat_mean=("lat_mean", "mean"), lat_p95=("lat_p95", "mean"),
        _n_idx=("n_idx", "sum"), _k_urr=("k_urr", "sum"), _nr_idx=("nr_idx", "sum"), _kr_urr=("kr_urr", "sum"),
        _n_ctx=("n_ctx", "sum"), _k_cer=("k_cer", "sum"), _nr_ctx=("nr_ctx", "sum"), _kr_cer=("kr_cer", "sum"),
        _chunks=("sum_chunks", tot), _chars=("sum_chars", tot)).reset_index()

    # Kaplan-Meier RPT (context side), censored trials included
    km = []
    for (m, c), g in res.groupby(["mode", "config"]):
        dur = g["rpt_ctx_s"].fillna(g["obs_s"])
        ev = ~g["censored_ctx"]
        km.append({"mode": m, "config": c, "km_median": km_quantile(dur, ev, 0.5),
                   "km_p95": km_quantile(dur, ev, 0.95),
                   "_rpt_filled": dur.mean()})      # censored -> observation time (a lower bound)
    summ = summ.merge(pd.DataFrame(km), on=["mode", "config"])

    def status(r):
        if r["cens_ctx"] == 0:
            return "enforced in every trial"
        if r["cens_ctx"] == r["trials"]:
            return f"never enforced (> {r['obs_mean']:.0f} s window)"
        return f"{int(r['cens_ctx'])} of {int(r['trials'])} not enforced in the window"
    summ["ctx_status"] = summ.apply(status, axis=1)

    # Interval rates are POOLED over all probes of the mode (a trial with 1 probe must not weigh
    # as much as one with 300; very short intervals, like event mode, make per-trial ratios noisy).
    nz = lambda c: summ[c].replace(0, np.nan)
    summ["urr_interval"] = summ["_k_urr"] / nz("_n_idx")
    summ["cer_interval"] = summ["_k_cer"] / nz("_n_ctx")
    summ["urr_rel"] = summ["_kr_urr"] / nz("_nr_idx")
    summ["cer_rel"] = summ["_kr_cer"] / nz("_nr_ctx")
    summ["rel_probes_ctx"] = summ["_nr_ctx"]
    summ["ctx_chunks"] = summ["_chunks"] / nz("_n_ctx")
    summ["ctx_chars"] = summ["_chars"] / nz("_n_ctx")
    summ["leaked_probes_per_trial"] = summ["_k_urr"] / summ["trials"]

    # Stale-state context exposure rate: how often a relevant question reaches the context while
    # the index is stale and nothing else filters. Measured where the stale period is long
    # (periodic, no-worker); used for modes whose own interval is too short to measure (event).
    ref = summ[(summ["mode"] == "periodic") | (summ["config"] == "no-worker")]
    if args.stale_rate is not None:
        stale_rate, stale_src = args.stale_rate, "given with --stale-rate"
    elif ref["_nr_ctx"].sum() >= MIN_REL_PROBES:
        stale_rate = ref["_kr_cer"].sum() / ref["_nr_ctx"].sum()
        stale_src = "pooled from " + ", ".join(f"{m}/{c}" for m, c in zip(ref["mode"], ref["config"]))
    else:
        stale_rate, stale_src = np.nan, "not available in this folder"

    # Expected exposures = queries/s x share about the document x mean RPT x chance it reaches the context.
    base = (args.qpm / 60.0) * args.share_relevant
    exp, basis = [], []
    for _, r in summ.iterrows():
        if r["cens_ctx"] == r["trials"]:
            exp.append(np.inf); basis.append("unbounded: revocation never enforced"); continue
        if r["_nr_ctx"] >= MIN_REL_PROBES:
            rate, b = r["cer_rel"], "measured"
        elif r["_n_ctx"] == 0:                       # no probe fell before enforcement
            rate, b = 0.0, "measured (blocked from the first probe)"
        elif not np.isnan(stale_rate):
            rate, b = stale_rate, f"estimated: window below probe resolution, stale-state rate {stale_rate:.3f}"
        else:
            rate, b = 1.0, "upper bound: no stale-state rate available"
        if r["cens_ctx"] > 0:
            b += f"; lower bound ({int(r['cens_ctx'])} censored)"
        exp.append(base * r["_rpt_filled"] * rate); basis.append(b)
    summ["exp_exposures"] = exp
    summ["exp_basis"] = basis
    summ = summ.drop(columns=[c for c in summ.columns if c.startswith("_")])
    summ.to_csv(f"{args.out}/summary.csv", index=False)

    print(f"kept {len(res)} trials, excluded {len(excl)}")
    print(pd.DataFrame(excl)["reason"].value_counts().to_string() if excl else "no exclusions")
    print(f"stale-state context rate: {stale_rate:.3f} ({stale_src})" if not np.isnan(stale_rate)
          else f"stale-state context rate: {stale_src}")
    cols = ["mode", "config", "trials", "cens_ctx", "km_median", "km_p95", "ctx_max", "urr_interval",
            "cer_interval", "urr_rel", "cer_rel", "rel_probes_ctx", "lat_mean", "exp_exposures"]
    print(summ[cols].round(3).to_string(index=False))
    for _, r in summ.iterrows():
        print(f"  {r['mode']}/{r['config']}: {r['ctx_status']}; exposures {r['exp_basis']}")


if __name__ == "__main__":
    main()
