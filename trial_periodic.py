import argparse
import csv
import os
import random
import threading
import time

import psycopg2
from qdrant_client import QdrantClient, models

PG = dict(host="localhost", port=5432, user="rag", password="ragpass", dbname="ragstudy")
QDRANT_URL = "http://localhost:6333"


def doc_filter(doc_id):
    return models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])


# ------------------------------------------------------------------ the sync layer
def sync_once(conn, client, collection):
    """Copy the access list of every changed document from Postgres into Qdrant."""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM documents WHERE acl_dirty")
        for (d,) in cur.fetchall():
            cur.execute("SELECT principal_type, principal_id FROM doc_acl WHERE doc_id = %s", (d,))
            principals = [f"{t}:{p}" for t, p in cur.fetchall()]
            client.set_payload(collection, payload={"allowed_principals": principals},
                               points=doc_filter(d))
            cur.execute("UPDATE documents SET acl_dirty = false WHERE doc_id = %s", (d,))
    conn.commit()


def sync_loop(interval, stop, collection):
    """Runs sync_once every `interval` seconds until `stop` is set."""
    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    next_run = time.monotonic() + interval
    while not stop.wait(max(0.0, next_run - time.monotonic())):
        sync_once(conn, client, collection)
        next_run += interval
    conn.close()


# ------------------------------------------------------------------ helpers
def pick_pair(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, principal_id FROM doc_acl "
                    "WHERE principal_type = 'user' ORDER BY random() LIMIT 1")
        return cur.fetchone()


def principals_for(conn, user):
    with conn.cursor() as cur:
        cur.execute("SELECT group_id FROM group_members WHERE user_id = %s", (user,))
        groups = [r[0] for r in cur.fetchall()]
    return ["public:*", f"user:{user}"] + [f"group:{g}" for g in groups]


def append_rows(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def count_rows(path):
    return sum(1 for _ in open(path)) - 1 if os.path.exists(path) else 0


# ------------------------------------------------------------------ one trial
def run_trial(a, conn, client):
    trial_id = f"periodic-{a.interval}s-{count_rows(f'{a.out}/trials.csv') + 1}"
    doc, user = pick_pair(conn)
    flt = models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])
    # use the document's own stored vector as the query, so it is the top hit while allowed
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

    _, got, status, _ = probe()
    if not (got == 1 and status == 200):                      # baseline failed
        append_rows(f"{a.out}/trials.csv",
                    ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"],
                    [[trial_id, "periodic", f"{a.interval}s", time.time(), 0, "ok"]])
        print(f"{trial_id}: baseline failed for {user} / {doc}, trial skipped")
        return

    stop = threading.Event()
    th = threading.Thread(target=sync_loop, args=(a.interval, stop, a.collection), daemon=True)
    th.start()
    time.sleep(random.uniform(0, a.interval))                 # revoke at a random point in the cycle

    with conn.cursor() as cur:                                # --- REVOKE at the source
        cur.execute("DELETE FROM doc_acl WHERE doc_id = %s AND principal_type = 'user' "
                    "AND principal_id = %s", (doc, user))
        cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
    conn.commit()
    t_rev = time.time()                                       # T_revocation

    window = a.window or (a.interval + 15)                    # same window for every trial
    attempts = []
    while time.time() < t_rev + window:
        t, got, status, ms = probe()
        # periodic mode: the gate trusts the index, so retrieved = reaches the prompt
        attempts.append([trial_id, t, got, got, status, round(ms, 1)])
        time.sleep(max(0.0, 0.1 - ms / 1000))

    # T_enforcement = start of the final run of blocked searches (needs at least 3)
    i = len(attempts)
    while i > 0 and attempts[i - 1][2] == 0 and attempts[i - 1][4] == 200:
        i -= 1
    stable = (len(attempts) - i) >= 3

    stop.set()
    th.join()
    with conn.cursor() as cur:                                # restore for the next trial
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "VALUES (%s, 'user', %s)", (doc, user))
        cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
    conn.commit()
    sync_once(conn, client, a.collection)

    append_rows(f"{a.out}/trials.csv",
                ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"],
                [[trial_id, "periodic", f"{a.interval}s", t_rev, 1, "ok"]])
    append_rows(f"{a.out}/attempts.csv",
                ["trial_id", "attempt_ts", "protected_returned", "protected_in_prompt",
                 "http_status", "latency_ms"], attempts)

    leaked = sum(x[2] for x in attempts)
    if stable:
        rpt = attempts[i][1] - t_rev
        print(f"{trial_id}: {user} / {doc}  RPT = {rpt:.2f} s   URR = {leaked / len(attempts):.2f}"
              f"   ({leaked} leaked of {len(attempts)} probes in a {window}s window)")
    else:
        print(f"{trial_id}: {user} / {doc}  NOT blocked within the window (censored)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30, help="sync interval in seconds")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--window", type=int, default=0,
                    help="observation window in seconds after revocation (default: interval + 15)")
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    with conn.cursor() as cur:                                # one-time, safe to repeat
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_dirty boolean DEFAULT false")
    conn.commit()
    for _ in range(a.trials):
        run_trial(a, conn, client)
    conn.close()


if __name__ == "__main__":
    main()