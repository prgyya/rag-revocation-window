import argparse
import os
import time

import psycopg2
from qdrant_client import QdrantClient, models

from trial_periodic import (PG, QDRANT_URL, append_rows, count_rows, doc_filter,
                            pick_pair, principals_for)


def allowed_docs(conn, user, doc_ids):
    """Authoritative check against Postgres: which of these docs may this user see right now?"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT doc_id FROM doc_acl
            WHERE doc_id = ANY(%s) AND (
                  principal_type = 'public'
               OR (principal_type = 'user'  AND principal_id = %s)
               OR (principal_type = 'group' AND principal_id IN
                   (SELECT group_id FROM group_members WHERE user_id = %s)))
        """, (list(doc_ids), user, user))
        ok = {r[0] for r in cur.fetchall()}
    conn.commit()                                   # end the read transaction
    return ok


def build_context(conn, hits, user, k_ctx=5):
    """Chunks that would be passed to the LLM, after the fresh authorization gate."""
    ok = allowed_docs(conn, user, {h.payload["doc_id"] for h in hits})
    return [h for h in hits if h.payload["doc_id"] in ok][:k_ctx]


def run_trial(a, conn, client):
    trial_id = f"{a.mode}-{count_rows(f'{a.out}/trials.csv') + 1}"
    doc, user = pick_pair(conn)
    flt = models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])
    vec = client.scroll(a.collection, scroll_filter=doc_filter(doc), limit=1,
                        with_vectors=True)[0][0].vector

    def probe():
        t, p0 = time.time(), time.perf_counter()
        try:
            # query-time: no index filter at all; hybrid: (stale) index filter as a coarse pre-filter
            hits = client.query_points(a.collection, query=vec,
                                       query_filter=flt if a.mode == "hybrid" else None,
                                       limit=a.fetch).points
            got = int(any(h.payload["doc_id"] == doc for h in hits))                    # URR
            ctx = int(any(h.payload["doc_id"] == doc
                          for h in build_context(conn, hits, user)))                    # CER
            status = 200
        except Exception:
            got, ctx, status = 0, 0, 500
        return t, got, status, (time.perf_counter() - p0) * 1000, ctx

    header = ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"]
    _, _, status, _, ctx = probe()
    if not (ctx == 1 and status == 200):
        append_rows(f"{a.out}/trials.csv", header, [[trial_id, a.mode, "gate", time.time(), 0, "ok"]])
        print(f"{trial_id}: baseline failed for {user} / {doc}, trial skipped")
        return

    with conn.cursor() as cur:                                # --- REVOKE in the authoritative store
        cur.execute("DELETE FROM doc_acl WHERE doc_id = %s AND principal_type = 'user' "
                    "AND principal_id = %s", (doc, user))
    conn.commit()
    t_rev = time.time()                                       # T_revocation (index is NOT updated)

    attempts = []
    while time.time() < t_rev + a.window:
        t, got, status, ms, ctx = probe()
        attempts.append([trial_id, t, got, ctx, status, round(ms, 1)])
        time.sleep(max(0.0, a.probe_ms / 1000 - ms / 1000))

    i = len(attempts)                                         # start of the final run with CER = 0
    while i > 0 and attempts[i - 1][3] == 0 and attempts[i - 1][4] == 200:
        i -= 1
    stable = (len(attempts) - i) >= 3

    with conn.cursor() as cur:                                # restore for the next trial
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "VALUES (%s, 'user', %s)", (doc, user))
    conn.commit()

    append_rows(f"{a.out}/trials.csv", header, [[trial_id, a.mode, "gate", t_rev, 1, "ok"]])
    append_rows(f"{a.out}/attempts.csv",
                ["trial_id", "attempt_ts", "protected_returned", "protected_in_prompt",
                 "http_status", "latency_ms"], attempts)

    urr = sum(x[2] for x in attempts) / len(attempts)
    cer = sum(x[3] for x in attempts) / len(attempts)
    if stable:
        print(f"{trial_id}: {user} / {doc}  RPT = {(attempts[i][1] - t_rev) * 1000:.0f} ms   "
              f"URR = {urr:.3f}   CER = {cer:.3f}   ({len(attempts)} probes)")
    else:
        print(f"{trial_id}: {user} / {doc}  NOT blocked within the window (censored)   "
              f"URR = {urr:.3f}   CER = {cer:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["query-time", "hybrid"], required=True)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--window", type=int, default=5, help="observation window, seconds")
    ap.add_argument("--probe-ms", type=int, default=10, help="target gap between probes")
    ap.add_argument("--fetch", type=int, default=20, help="chunks fetched from the index before the gate")
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default="results_gate")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    for _ in range(a.trials):
        run_trial(a, conn, client)
    conn.close()


if __name__ == "__main__":
    main()