import argparse
import os

import numpy as np
import pandas as pd

ORDER = ["periodic", "event", "query-time", "hybrid"]
BLUE, ORANGE = "#2a78d6", "#eb6834"                         # categorical slots 1 and 2
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def label(mode, config):
    if config == "no-worker":
        return "control (no worker)"
    if mode == "periodic":
        return f"periodic ({config} sync)"
    if config.startswith("gate-ttl"):
        return f"{mode} (cache {config[8:]})"
    return mode


def boot_ratio(df, k, n, B, rng):
    """95% CI of sum(k)/sum(n), resampling whole TRIALS (probes within a trial are correlated)."""
    if len(df) < 2 or df[n].sum() == 0:
        return np.nan, np.nan
    K, N = df[k].to_numpy(float), df[n].to_numpy(float)
    vals = []
    for _ in range(B):
        i = rng.integers(0, len(df), len(df))
        if N[i].sum() > 0:
            vals.append(K[i].sum() / N[i].sum())
    return tuple(np.percentile(vals, [2.5, 97.5])) if vals else (np.nan, np.nan)


def boot_mean(x, B, rng):
    x = np.asarray(x, float)
    if len(x) < 2:
        return np.nan, np.nan
    return tuple(np.percentile([x[rng.integers(0, len(x), len(x))].mean() for _ in range(B)], [2.5, 97.5]))


def fmt_s(v):
    if pd.isna(v):
        return "n/a"
    if np.isinf(v):
        return "not reached"
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.2f} s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+", help="output folders of clean_logs.py")
    ap.add_argument("--out", default="report_tables")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = np.random.default_rng(a.seed)

    trials = pd.concat([pd.read_csv(f"{f}/clean_trials.csv") for f in a.folders], ignore_index=True)
    summ = pd.concat([pd.read_csv(f"{f}/summary.csv") for f in a.folders], ignore_index=True)
    if summ.duplicated(["mode", "config"]).any():
        raise SystemExit("the same mode/config appears in two folders; clean them together or pass one")
    summ["label"] = [label(m, c) for m, c in zip(summ["mode"], summ["config"])]
    summ["_o"] = [ORDER.index(m) * 10 + (5 if c == "no-worker" else 0) for m, c in zip(summ["mode"], summ["config"])]
    summ = summ.sort_values("_o").reset_index(drop=True)

    # ---------------------------------------------------------------- final table
    rows = []
    for _, s in summ.iterrows():
        g = trials[(trials["mode"] == s["mode"]) & (trials["config"] == s["config"])]
        filled = g["rpt_ctx_s"].fillna(g["obs_s"])
        u_lo, u_hi = boot_ratio(g, "kr_urr", "nr_idx", a.boot, rng)
        c_lo, c_hi = boot_ratio(g, "kr_cer", "nr_ctx", a.boot, rng)
        r_lo, r_hi = boot_mean(filled, a.boot, rng)
        all_cens = s["cens_ctx"] == s["trials"]
        if s["rel_probes_ctx"] >= 30:
            ctx_rate = f"{s['cer_rel']:.3f} [{c_lo:.3f}, {c_hi:.3f}]"
        elif str(s["exp_basis"]).startswith("measured (blocked"):
            ctx_rate = "0 (blocked from the first probe)"
        else:
            ctx_rate = f"too few probes to measure ({int(s['rel_probes_ctx'])}); see basis"
        rows.append({
            "configuration": s["label"], "trials": s["trials"], "not enforced": s["cens_ctx"],
            "RPT median (KM)": fmt_s(s["km_median"]), "RPT p95 (KM)": fmt_s(s["km_p95"]),
            "RPT max": "never enforced" if all_cens else fmt_s(s["ctx_max"]),
            "RPT mean [95% CI]": f"> {fmt_s(filled.mean())} (never enforced)" if all_cens
                else f"{fmt_s(filled.mean())} [{fmt_s(r_lo)}, {fmt_s(r_hi)}]"
                     + (" (lower bound)" if s["cens_ctx"] > 0 else ""),
            "index exposure, relevant queries [95% CI]":
                "n/a" if pd.isna(s["urr_rel"]) else f"{s['urr_rel']:.3f} [{u_lo:.3f}, {u_hi:.3f}]",
            "context exposure, relevant queries [95% CI]": ctx_rate,
            "probe latency mean (ms)": round(s["lat_mean"], 1),
            "expected exposures per revocation": "unbounded" if np.isinf(s["exp_exposures"])
                else f"{s['exp_exposures']:.2g}",
            "basis": s["exp_basis"],
        })
    table = pd.DataFrame(rows)
    table.to_csv(f"{a.out}/final_table.csv", index=False)

    # ---------------------------------------------------------------- sensitivity of expected exposures
    # exp_exposures is linear in qpm x share, so rescale the default (qpm 1, share 0.1)
    sens = []
    for _, s in summ.iterrows():
        for qpm in (0.2, 1, 5):
            for share in (0.05, 0.1, 0.3):
                v = s["exp_exposures"] * (qpm / 1.0) * (share / 0.1)
                sens.append({"configuration": s["label"], "queries per minute": qpm,
                             "share about the document": share,
                             "expected exposures": "unbounded" if np.isinf(v) else round(v, 6)})
    pd.DataFrame(sens).to_csv(f"{a.out}/sensitivity.csv", index=False)
    print("note: sensitivity.csv assumes clean_logs.py ran with the defaults --qpm 1 --share-relevant 0.1")

    plot_rpt(trials, summ, a.out)
    plot_rates(trials, summ, a.out, a.boot, rng)
    with pd.option_context("display.max_colwidth", 60, "display.width", 250):
        print(table.drop(columns=["basis"]).to_string(index=False))
    print(f"\nwritten to ./{a.out}/")


# ---------------------------------------------------------------- charts
def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_rpt(trials, summ, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    floor = 1e-3                                       # 0 ms cannot go on a log axis: shown at 1 ms
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(summ) + 1.4), facecolor=SURFACE)
    style(ax)
    for y, (_, s) in enumerate(summ.iterrows()):
        g = trials[(trials["mode"] == s["mode"]) & (trials["config"] == s["config"])]
        jit = np.random.default_rng(y).uniform(-0.18, 0.18, len(g))
        ok = ~g["censored_ctx"].to_numpy()
        x = np.maximum(g["rpt_ctx_s"].fillna(g["obs_s"]).to_numpy(), floor)
        ax.scatter(x[ok], y + jit[ok], s=22, color=BLUE, edgecolor=SURFACE, linewidth=1, zorder=3)
        if (~ok).any():
            ax.scatter(x[~ok], y + jit[~ok], s=22, facecolor="none", edgecolor=BLUE, linewidth=1.2, zorder=3)
            ax.annotate("never enforced →", (x[~ok].max(), y), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8.5, color=INK2)
        if ok.any():
            ax.plot([np.median(x[ok])] * 2, [y - 0.3, y + 0.3], color=INK, linewidth=2, zorder=4)
    ax.set_xscale("log")
    ax.set_yticks(range(len(summ)), summ["label"], color=INK, fontsize=9.5)
    ax.invert_yaxis()
    ax.set_xlabel("time from revocation to enforcement in the LLM context (log scale; 0 shown at 1 ms)",
                  color=INK2, fontsize=9)
    ax.set_title("Revocation propagation time per trial", loc="left", color=INK, fontsize=12, pad=12)
    ax.text(0, 1.01, "dots = trials, black tick = median, hollow = not enforced within the window",
            transform=ax.transAxes, fontsize=8.5, color=INK2)
    fig.tight_layout()
    fig.savefig(f"{out}/rpt_by_mode.png", dpi=200)
    plt.close(fig)


def plot_rates(trials, summ, out, B, rng):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 0.75 * len(summ) + 1.4), facecolor=SURFACE)
    style(ax)
    h = 0.34
    for y, (_, s) in enumerate(summ.iterrows()):
        g = trials[(trials["mode"] == s["mode"]) & (trials["config"] == s["config"])]
        for off, (k, n, col, color, name) in zip(
                (-h / 2 - 0.01, h / 2 + 0.01),
                (("kr_urr", "nr_idx", "urr_rel", BLUE, "index returned it"),
                 ("kr_cer", "nr_ctx", "cer_rel", ORANGE, "reached the LLM context"))):
            v = s[col]
            if g[n].sum() < 30:                       # too few relevant probes to show a rate
                txt = "0 (blocked from the first probe)" if str(s["exp_basis"]).startswith("measured (blocked") \
                    else "too few probes to measure"
                ax.text(0.01, y + off, txt, va="center", fontsize=8.5, color=INK2)
                continue
            if pd.isna(v):
                continue
            lo, hi = boot_ratio(g, k, n, B, rng)
            ax.barh(y + off, v, height=h, color=color, label=name if y == 0 or name not in
                    ax.get_legend_handles_labels()[1] else None)
            if not pd.isna(lo):
                ax.plot([lo, hi], [y + off] * 2, color=INK, linewidth=1.2)
            ax.text(max(v, hi if not pd.isna(hi) else v) + 0.015, y + off, f"{v:.2f}", va="center",
                    fontsize=8.5, color=INK)
    ax.set_xlim(0, 1.12)
    ax.set_yticks(range(len(summ)), summ["label"], color=INK, fontsize=9.5)
    ax.set_ylim(len(summ) - 0.35, -0.6)
    ax.set_xlabel("share of relevant queries, while the revocation was not yet enforced", color=INK2, fontsize=9)
    ax.set_title("How often a revoked user's relevant question leaked", loc="left", color=INK, fontsize=12, pad=24)
    handles, labels = ax.get_legend_handles_labels()
    seen = dict(zip(labels, handles))
    ax.legend(seen.values(), seen.keys(), loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False,
              fontsize=8.5, labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(f"{out}/exposure_rates.png", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    main()
