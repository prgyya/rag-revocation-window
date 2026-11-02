import sys

import pandas as pd

a = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else "results_test_v3b/attempts.csv")
stale = a[(a["ref_returned"] == 1) & (a["http_status"] == 200)]
t = stale.groupby("relevant")[["protected_returned", "protected_in_prompt"]].agg(["mean", "count"])
print(t.round(3).to_string())
if 0 in stale["relevant"].values:
    r = stale.loc[stale["relevant"] == 0, "protected_returned"].mean()
    n = int((stale["relevant"] == 0).sum())
    verdict = "OK, keep the data" if r < 0.05 else "HIGH: consider --source 20ng for the final runs"
    print(f"\nunrelated questions that returned the revoked document: {r:.1%} of {n} probes -> {verdict}")
