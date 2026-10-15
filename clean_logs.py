import argparse, os
import numpy as np
import pandas as pd

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
    warm = trials.groupby(["mode", "config"]).head(args.warmup)["trial_id"]
    drop(warm, "warm-up trial")
    trials = trials[~trials.trial_id.isin(warm)]

    # 4. attempts: keep only post-revocation attempts of surviving trials
    att = att.merge(trials[["trial_id", "t_revocation"]], on="trial_id", how="inner")
    att = att[att["attempt_ts"] >= att["t_revocation"]]
    att = att.sort_values(["trial_id", "attempt_ts"])

    rows = []
    for tid, g in att.groupby("trial_id"):
        t_rev = g["t_revocation"].iloc[0]
        err = (g["http_status"] != 200).mean()
        if len(g) < args.k:
            drop([tid], "too few attempts"); continue
        if err > args.max_error_rate:
            drop([tid], f"error rate {err:.1%} > {args.max_error_rate:.0%}"); continue
        g = g[g["http_status"] == 200]                              # errors are not 'blocked'

        t_idx = stable_block_time(g, args.k, "protected_returned")
        t_ctx = stable_block_time(g, args.k, "protected_in_prompt")
        rpt_idx = np.nan if np.isnan(t_idx) else t_idx - t_rev
        rpt_ctx = np.nan if np.isnan(t_ctx) else t_ctx - t_rev
        if (rpt_idx < 0) or (rpt_ctx < 0):                          # NaN compares False
            drop([tid], "negative RPT (clock/order problem)"); continue

        w = g[g["attempt_ts"] <= t_rev + args.window]
        rows.append({
            "trial_id": tid,
            "rpt_index_s": rpt_idx, "censored_index": bool(np.isnan(rpt_idx)),
            "rpt_ctx_s": rpt_ctx, "censored_ctx": bool(np.isnan(rpt_ctx)),
            "urr": w["protected_returned"].mean() if len(w) else np.nan,
            "cer": w["protected_in_prompt"].mean() if len(w) else np.nan,
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
    summ = res.groupby(["mode", "config"]).agg(
        trials=("trial_id", "count"),
        cens_idx=("censored_index", "sum"), cens_ctx=("censored_ctx", "sum"),
        idx_mean=("rpt_index_s", "mean"), idx_p95=("rpt_index_s", q(0.95)),
        ctx_mean=("rpt_ctx_s", "mean"), ctx_median=("rpt_ctx_s", "median"),
        ctx_p95=("rpt_ctx_s", q(0.95)), ctx_p99=("rpt_ctx_s", q(0.99)), ctx_max=("rpt_ctx_s", "max"),
        urr_mean=("urr", "mean"), cer_mean=("cer", "mean")).reset_index()
    summ.to_csv(f"{args.out}/summary.csv", index=False)

    print(f"kept {len(res)} trials, excluded {len(excl)}")
    print(pd.DataFrame(excl)["reason"].value_counts().to_string() if excl else "no exclusions")
    print(summ.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
