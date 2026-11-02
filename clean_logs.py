import argparse, os
import numpy as np
import pandas as pd

# Two RPT definitions are reported for EVERY mode, so URR and CER keep their own meaning:
#   rpt_index_s : time until the INDEX stops returning the protected chunk   (goes with URR)
#   rpt_ctx_s   : time until the chunk stops reaching the LLM CONTEXT        (goes with CER)
# For periodic/event there is no gate, so the two coincide. For query-time/hybrid the index
# stays stale on purpose, so rpt_index is censored while rpt_ctx is ~0.
# URR-side interval measures end at index enforcement; CER-side ones end at context enforcement.


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", required=True)
    ap.add_argument("--attempts", required=True)
    ap.add_argument("--out", default="clean")
    ap.add_argument("--k", type=int, default=3, help="consecutive blocked attempts = enforced")
    ap.add_argument("--warmup", type=int, default=3, help="trials to drop per mode+config")
    ap.add_argument("--window", type=float, default=120.0,
                    help="fixed observation window (s) after revocation for URR/CER")
    ap.add_argument("--max-error-rate", type=float, default=0.05)
    ap.add_argument("--qpm", type=float, default=1.0,
                    help="ASSUMED queries per minute from the revoked user (for expected exposures)")
    ap.add_argument("--share-relevant", type=float, default=0.1,
                    help="ASSUMED share of those queries that are about the revoked document")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    trials = pd.read_csv(args.trials)
    att = pd.read_csv(args.attempts)
    excl = []

    def drop(ids, reason):
        for t in ids:
            excl.append({"trial_id": t, "reason": reason})

    # 1. basic integrity: duplicates, missing values
    dup = trials[trials.duplicated("trial_id", keep="first")]["trial_id"]
    drop(dup, "duplicate trial_id")
    trials = trials.drop_duplicates("trial_id", keep="first")
    att = att.drop_duplicates()
    bad = trials[trials[["mode", "config", "t_revocation"]].isna().any(axis=1)]["trial_id"]
    drop(bad, "missing required field")
    trials = trials[~trials.trial_id.isin(bad)]

    # 2. trials that failed before measuring anything useful
    f = trials[trials["baseline_ok"] != 1]["trial_id"]
    drop(f, "baseline check failed (user could not retrieve protected doc)")
    trials = trials[~trials.trial_id.isin(f)]
    f = trials[trials["status"] != "ok"]["trial_id"]
    drop(f, "service crashed / timed out")
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
        if (rpt_idx < 0) or (rpt_ctx < 0):                          # NaN compares False
            drop([tid], "negative RPT (clock/order problem)"); continue

        w = g[g["attempt_ts"] <= t_rev + args.window]
        has_rel = "relevant" in w.columns

        def interval(t_stop, col):
            """Probes from revocation until t_stop: all of them, and only the ones about the
            protected document. Returns the probes, then counts (n, hits, n_rel, hits_rel)."""
            o = w[w["attempt_ts"] < t_stop]
            r = o[o["relevant"] == 1] if has_rel else o
            return o, len(o), int(o[col].sum()), len(r), int(r[col].sum())

        # The URR side ends when the INDEX stops returning the chunk (never, in the gate modes);
        # the CER side ends when the chunk stops reaching the LLM CONTEXT.
        o_idx, n_idx, k_urr, nr_idx, kr_urr = interval(np.inf if np.isnan(t_idx) else t_idx, "protected_returned")
        o_ctx, n_ctx, k_cer, nr_ctx, kr_cer = interval(np.inf if np.isnan(t_ctx) else t_ctx, "protected_in_prompt")
        div = lambda a_, b_: a_ / b_ if b_ else np.nan
        rows.append({
            "trial_id": tid,
            "rpt_index_s": rpt_idx, "censored_index": bool(np.isnan(rpt_idx)),
            "rpt_ctx_s": rpt_ctx, "censored_ctx": bool(np.isnan(rpt_ctx)),
            "urr": w["protected_returned"].mean() if len(w) else np.nan,
            "cer": w["protected_in_prompt"].mean() if len(w) else np.nan,
            "urr_interval": div(k_urr, n_idx), "cer_interval": div(k_cer, n_ctx),
            "urr_rel_interval": div(kr_urr, nr_idx), "cer_rel_interval": div(kr_cer, nr_ctx),
            "leaked_probes": k_urr,
            "n_idx": n_idx, "k_urr": k_urr, "nr_idx": nr_idx, "kr_urr": kr_urr,
            "n_ctx": n_ctx, "k_cer": k_cer, "nr_ctx": nr_ctx, "kr_cer": kr_cer,
            "sum_chunks": float(o_ctx["ctx_chunks"].sum()) if "ctx_chunks" in o_ctx.columns else np.nan,
            "sum_chars": float(o_ctx["ctx_chars"].sum()) if "ctx_chars" in o_ctx.columns else np.nan,
            "lat_mean": w["latency_ms"].mean() if len(w) else np.nan,
            "lat_p95": w["latency_ms"].quantile(0.95) if len(w) else np.nan,
            "n_attempts": len(w), "error_rate": err,
        })

    if not rows:
        print("no trials survived cleaning"); return
    res = pd.DataFrame(rows).merge(trials[["trial_id", "mode", "config"]], on="trial_id")
    missing = set(trials.trial_id) - set(res.trial_id) - {e["trial_id"] for e in excl}
    drop(missing, "no valid post-revocation attempts")

    res.to_csv(f"{args.out}/clean_trials.csv", index=False)
    pd.DataFrame(excl).to_csv(f"{args.out}/exclusions.csv", index=False)

    # 5. summary. Outliers are KEPT (tail values are the finding); censored are counted, not hidden
    def q(p): return lambda s: s.quantile(p)
    def tot(s): return s.sum(min_count=1)
    summ = res.groupby(["mode", "config"]).agg(
        trials=("trial_id", "count"),
        cens_idx=("censored_index", "sum"), cens_ctx=("censored_ctx", "sum"),
        idx_mean=("rpt_index_s", "mean"), idx_p95=("rpt_index_s", q(0.95)),
        ctx_mean=("rpt_ctx_s", "mean"), ctx_median=("rpt_ctx_s", "median"),
        ctx_p95=("rpt_ctx_s", q(0.95)), ctx_p99=("rpt_ctx_s", q(0.99)), ctx_max=("rpt_ctx_s", "max"),
        urr_mean=("urr", "mean"), cer_mean=("cer", "mean"),
        leaked_probes=("k_urr", "mean"),
        lat_mean=("lat_mean", "mean"), lat_p95=("lat_p95", "mean"),
        _n_idx=("n_idx", "sum"), _k_urr=("k_urr", "sum"), _nr_idx=("nr_idx", "sum"), _kr_urr=("kr_urr", "sum"),
        _n_ctx=("n_ctx", "sum"), _k_cer=("k_cer", "sum"), _nr_ctx=("nr_ctx", "sum"), _kr_cer=("kr_cer", "sum"),
        _chunks=("sum_chunks", tot), _chars=("sum_chars", tot)).reset_index()
    # Interval rates are POOLED over all probes of the mode (a trial with 1 probe must not weigh
    # as much as one with 300; very short intervals, like event mode, make per-trial ratios noisy).
    nz = lambda c: summ[c].replace(0, np.nan)
    summ["urr_interval"] = summ["_k_urr"] / nz("_n_idx")
    summ["cer_interval"] = summ["_k_cer"] / nz("_n_ctx")
    summ["urr_rel"] = summ["_kr_urr"] / nz("_nr_idx")
    summ["cer_rel"] = summ["_kr_cer"] / nz("_nr_ctx")
    summ["ctx_chunks"] = summ["_chunks"] / nz("_n_ctx")
    summ["ctx_chars"] = summ["_chars"] / nz("_n_ctx")
    # Expected exposures = queries/s x share about the document x mean RPT x chance it reaches the context.
    # If no relevant probe fell inside any interval, the chance is unknown: use 1 (an upper bound) and flag it.
    summ["exp_upper_bound"] = summ["cer_rel"].isna() & summ["ctx_mean"].notna()
    summ["exp_exposures"] = (args.qpm / 60.0) * args.share_relevant * summ["ctx_mean"] * summ["cer_rel"].fillna(1.0)
    summ = summ.drop(columns=[c for c in summ.columns if c.startswith("_")])
    summ.to_csv(f"{args.out}/summary.csv", index=False)

    print(f"kept {len(res)} trials, excluded {len(excl)}")
    print(pd.DataFrame(excl)["reason"].value_counts().to_string() if excl else "no exclusions")
    print(summ.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
