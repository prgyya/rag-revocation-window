import argparse
import os
import time

import psycopg2
from qdrant_client import QdrantClient, models

from trial_periodic import (ATTEMPT_HEADER, CTX_K, PG, QDRANT_URL, TRIAL_HEADER, append_rows,
                            attempt_row, count_rows, doc_filter, final_blocked_run,
                            load_pool, make_prober, pick_pair, principals_for)


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


def run_trial(a, conn, client, pool):
    trial_id = f"{a.mode}-{count_rows(f'{a.out}/trials.csv') + 1}"
    doc, user = pick_pair(conn)
    flt = models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])
    vec = client.scroll(a.collection, scroll_filter=doc_filter(doc), limit=1,
                        with_vectors=True)[0][0].vector

    def gate_context(hits):
        """Chunks that would go to the LLM, after the fresh authorization gate."""
        ok = allowed_docs(conn, user, {h.payload["doc_id"] for h in hits})
        return [h for h in hits if h.payload["doc_id"] in ok][:CTX_K]

    probe = make_prober(client, a.collection, flt if a.mode == "hybrid" else None, doc, vec,
                        pool, a.min_alpha, gate_context, on_error=conn.rollback)

    _, _, _, r_got, r_ctx, status, _ = probe(vary=False)
    if not (r_ctx == 1 and status == 200):
        append_rows(f"{a.out}/trials.csv", TRIAL_HEADER, [[trial_id, a.mode, "gate", time.time(), 0, "ok"]])
        print(f"{trial_id}: baseline failed for {user} / {doc}, trial skipped")
        return

    with conn.cursor() as cur:                                # --- REVOKE in the authoritative store
        cur.execute("DELETE FROM doc_acl WHERE doc_id = %s AND principal_type = 'user' "
                    "AND principal_id = %s", (doc, user))
    conn.commit()
    t_rev = time.time()                                       # T_revocation (index is NOT updated)

    attempts = []
    while time.time() < t_rev + a.window:
        p = probe()
        attempts.append(attempt_row(trial_id, p))
        time.sleep(max(0.0, a.probe_ms / 1000 - p[6] / 1000))
    i, stable = final_blocked_run(attempts)

    with conn.cursor() as cur:                                # restore for the next trial
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "VALUES (%s, 'user', %s)", (doc, user))
    conn.commit()

    append_rows(f"{a.out}/trials.csv", TRIAL_HEADER, [[trial_id, a.mode, "gate", t_rev, 1, "ok"]])
    append_rows(f"{a.out}/attempts.csv", ATTEMPT_HEADER, attempts)

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
    ap.add_argument("--min-alpha", type=float, default=0.3,
                    help="lowest relevance of a workload search to the protected document (0-1)")
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default="results_gate")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    pool = load_pool(client, a.collection)
    for _ in range(a.trials):
        run_trial(a, conn, client, pool)
    conn.close()


if __name__ == "__main__":
    main()
