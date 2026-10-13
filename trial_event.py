import argparse
import os
import select
import threading
import time

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from qdrant_client import QdrantClient, models

from trial_periodic import (PG, QDRANT_URL, append_rows, count_rows, doc_filter,
                            pick_pair, principals_for)


def sync_doc(cur, client, collection, doc):
    """Copy one document's access list from Postgres into Qdrant."""
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
                sync_doc(cur, client, collection, conn.notifies.pop(0).payload)
    conn.close()


def run_trial(a, conn, client):
    # CHANGED: label depends on whether the worker is running ("notify" or "no-worker")
    config = "no-worker" if a.no_worker else "notify"
    trial_id = f"event-{config}-{count_rows(f'{a.out}/trials.csv') + 1}"
    doc, user = pick_pair(conn)
    flt = models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])
    vec = client.scroll(a.collection, scroll_filter=doc_filter(doc), limit=1,
                        with_vectors=True)[0][0].vector

    def probe():
        t, p0 = time.time(), time.perf_counter()
        try:
            hits = client.query_points(a.collection, query=vec, query_filter=flt, limit=5).points
            got, status = int(any(h.payload["doc_id"] == doc for h in hits)), 200
        except Exception:
            got, status = 0, 500
        return t, got, status, (time.perf_counter() - p0) * 1000

    header = ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"]
    _, got, status, _ = probe()
    if not (got == 1 and status == 200):
        append_rows(f"{a.out}/trials.csv", header, [[trial_id, "event", config, time.time(), 0, "ok"]])
        print(f"{trial_id}: baseline failed for {user} / {doc}, trial skipped")
        return

    # CHANGED: the listener thread is only started in normal mode
    stop, ready, th = threading.Event(), threading.Event(), None
    if not a.no_worker:
        th = threading.Thread(target=event_worker, args=(stop, ready, a.collection), daemon=True)
        th.start()
        ready.wait()
    time.sleep(0.5)                                           # let LISTEN settle

    with conn.cursor() as cur:                                # --- REVOKE + publish the event
        cur.execute("DELETE FROM doc_acl WHERE doc_id = %s AND principal_type = 'user' "
                    "AND principal_id = %s", (doc, user))
        cur.execute("SELECT pg_notify('acl_changed', %s)", (doc,))
    conn.commit()
    t_rev = time.time()                                       # T_revocation

    attempts = []
    while time.time() < t_rev + a.window:
        t, got, status, ms = probe()
        attempts.append([trial_id, t, got, got, status, round(ms, 1)])
        time.sleep(max(0.0, a.probe_ms / 1000 - ms / 1000))

    i = len(attempts)                                         # start of the final blocked run
    while i > 0 and attempts[i - 1][2] == 0 and attempts[i - 1][4] == 200:
        i -= 1
    stable = (len(attempts) - i) >= 3

    stop.set()
    if th is not None:                                        # CHANGED: nothing to join in no-worker mode
        th.join()
    with conn.cursor() as cur:                                # restore for the next trial
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "VALUES (%s, 'user', %s)", (doc, user))
        sync_doc(cur, client, a.collection, doc)
    conn.commit()

    append_rows(f"{a.out}/trials.csv", header, [[trial_id, "event", config, t_rev, 1, "ok"]])
    append_rows(f"{a.out}/attempts.csv",
                ["trial_id", "attempt_ts", "protected_returned", "protected_in_prompt",
                 "http_status", "latency_ms"], attempts)

    leaked = sum(x[2] for x in attempts)
    if stable:
        print(f"{trial_id}: {user} / {doc}  RPT = {(attempts[i][1] - t_rev) * 1000:.0f} ms   "
              f"URR = {leaked / len(attempts):.3f}   ({leaked} leaked of {len(attempts)} probes)")
    else:
        print(f"{trial_id}: {user} / {doc}  NOT blocked within the window (censored)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--window", type=int, default=15, help="observation window, seconds")
    ap.add_argument("--probe-ms", type=int, default=10, help="target gap between probes")
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default="results")
    # CHANGED: new control option
    ap.add_argument("--no-worker", action="store_true",
                    help="control run: revoke and send the event, but start no listener")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    for _ in range(a.trials):
        run_trial(a, conn, client)
    conn.close()


if __name__ == "__main__":
    main()