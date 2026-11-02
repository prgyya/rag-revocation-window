import argparse
import os
import select
import threading

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from qdrant_client import QdrantClient

from trial_periodic import (PG, QDRANT_URL, add_common_args, build_context, doc_filter,
                            doc_vector, ensure_schema, load_pool, make_prober, new_trial_id,
                            observe, pick_pair, print_trial, restore_grant, revoke_grant,
                            save_baseline_failure, save_trial, seed_everything, user_filter)


def sync_doc(cur, client, collection, doc):
    """Copy one document's access list from Postgres into Qdrant.
    The dirty flag is cleared first, so a change that lands meanwhile sets it again."""
    cur.execute("UPDATE documents SET acl_dirty = false WHERE doc_id = %s", (doc,))
    cur.execute("SELECT principal_type, principal_id FROM doc_acl WHERE doc_id = %s", (doc,))
    principals = [f"{t}:{p}" for t, p in cur.fetchall()]
    client.set_payload(collection, payload={"allowed_principals": principals}, points=doc_filter(doc))


def event_worker(stop, ready, collection):
    """Listens for 'acl_changed' events and updates Qdrant as soon as one arrives."""
    conn = psycopg2.connect(**PG)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("LISTEN acl_changed")
    client = QdrantClient(url=QDRANT_URL)
    ready.set()
    while not stop.is_set():
        if select.select([conn], [], [], 0.01)[0]:
            conn.poll()
            while conn.notifies:
                doc = conn.notifies.pop(0).payload
                try:
                    sync_doc(cur, client, collection, doc)
                except Exception as e:          # the event is lost, but acl_dirty stays set
                    print(f"[worker] sync of {doc} failed: {e!r}", flush=True)
                    cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
    conn.close()


def run_trial(a, conn, client, pool):
    config = "no-worker" if a.no_worker else "notify"
    trial_id = new_trial_id(f"event-{config}", a.out)
    doc, user = pick_pair(conn)
    probe = make_prober(client, a.collection, user_filter(conn, user), doc,
                        doc_vector(client, a.collection, doc), pool, a.min_alpha, build_context,
                        p_relevant=a.p_relevant)

    _, _, _, r_got, r_ctx, status, _ = probe(vary=False)
    if not (r_ctx == 1 and status == 200):
        save_baseline_failure(a.out, trial_id, "event", config, user, doc)
        return

    stop, ready, th = threading.Event(), threading.Event(), None
    t_rev, attempts, result = None, [], "crashed"
    try:
        if not a.no_worker:                                   # control run: nobody listens
            th = threading.Thread(target=event_worker, args=(stop, ready, a.collection), daemon=True)
            th.start()
            ready.wait()
        stop.wait(0.5)                                        # let LISTEN settle
        # REVOKE + durable dirty flag + event, all in one transaction
        t_rev = revoke_grant(conn, doc, user, mark_dirty=True, notify=True)
        attempts = observe(trial_id, probe, t_rev, a.window, a.probe_ms)
        result = "ok"
    finally:                                                  # always restore
        stop.set()
        if th is not None:
            th.join()
        restore_grant(conn, doc, user, mark_dirty=False)
        with conn.cursor() as cur:
            sync_doc(cur, client, a.collection, doc)
        conn.commit()
        if t_rev is not None:
            save_trial(a.out, trial_id, "event", config, t_rev, result, attempts)
    print_trial(trial_id, user, doc, attempts, t_rev, ms=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=35, help="observation window, seconds")
    ap.add_argument("--probe-ms", type=int, default=10, help="target gap between probes")
    ap.add_argument("--no-worker", action="store_true",
                    help="control run: revoke and send the event, but start no listener")
    add_common_args(ap, "results")
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
