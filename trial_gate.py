import argparse
import os
import random
import time

import psycopg2
from qdrant_client import QdrantClient

from trial_periodic import (CTX_K, PG, QDRANT_URL, add_common_args, doc_vector, ensure_schema,
                            load_pool, make_prober, new_trial_id, observe, pick_pair, print_trial,
                            restore_grant, revoke_grant, save_baseline_failure, save_trial,
                            seed_everything, user_filter)


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


def make_gate(gate_conn, user, ttl):
    """The authorization check. ttl = 0: ask Postgres on every query.
    ttl > 0: cache each (user, document) decision for ttl seconds, as many real
    authorization services do; a revocation then takes up to ttl seconds to bite."""
    cache = {}

    def allowed(doc_ids):
        if ttl <= 0:
            return allowed_docs(gate_conn, user, doc_ids)
        now = time.monotonic()
        stale = [d for d in doc_ids if d not in cache or now - cache[d][1] > ttl]
        if stale:
            ok = allowed_docs(gate_conn, user, stale)
            for d in stale:
                cache[d] = (d in ok, now)
        return {d for d in doc_ids if cache[d][0]}

    return allowed


def run_trial(a, conn, client, pool):
    config = f"gate-ttl{a.cache_ttl}s" if a.cache_ttl > 0 else "gate"
    trial_id = new_trial_id(f"{a.mode}-{config}", a.out)
    doc, user = pick_pair(conn)
    gate_conn = psycopg2.connect(**PG)              # the gate is its own service, not the admin's session
    allowed = make_gate(gate_conn, user, a.cache_ttl)

    def gate_context(hits):
        """Chunks that would go to the LLM, after the authorization gate."""
        ok = allowed({h.payload["doc_id"] for h in hits})
        return [h for h in hits if h.payload["doc_id"] in ok][:CTX_K]

    flt = user_filter(conn, user) if a.mode == "hybrid" else None
    probe = make_prober(client, a.collection, flt, doc, doc_vector(client, a.collection, doc),
                        pool, a.min_alpha, gate_context, on_error=gate_conn.rollback,
                        p_relevant=a.p_relevant)

    _, _, _, r_got, r_ctx, status, _ = probe(vary=False)
    if not (r_ctx == 1 and status == 200):
        save_baseline_failure(a.out, trial_id, a.mode, config, user, doc)
        gate_conn.close()
        return

    t_rev, attempts, result = None, [], "crashed"
    try:
        if a.cache_ttl > 0:                         # revoke at a random point of the cache lifetime
            time.sleep(random.uniform(0, a.cache_ttl))
        # REVOKE in the authoritative store only; the index is deliberately NOT updated
        t_rev = revoke_grant(conn, doc, user, mark_dirty=False)
        attempts = observe(trial_id, probe, t_rev, a.window, a.probe_ms)
        result = "ok"
    finally:                                        # always restore
        restore_grant(conn, doc, user, mark_dirty=False)
        gate_conn.close()
        if t_rev is not None:
            save_trial(a.out, trial_id, a.mode, config, t_rev, result, attempts)
    print_trial(trial_id, user, doc, attempts, t_rev, ms=a.cache_ttl == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["query-time", "hybrid"], required=True)
    ap.add_argument("--window", type=int, default=35, help="observation window, seconds")
    ap.add_argument("--probe-ms", type=int, default=10, help="target gap between probes")
    ap.add_argument("--cache-ttl", type=int, default=0,
                    help="optional: cache authorization decisions for this many seconds (0 = off)")
    add_common_args(ap, "results_gate")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    ensure_schema(conn)
    seed_everything(conn, a.seed)
    pool = load_pool(client, a.collection)
    for _ in range(a.trials):
        run_trial(a, conn, client, pool)
    conn.close()


if __name__ == "__main__":
    main()
