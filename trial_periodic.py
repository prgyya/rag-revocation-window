import argparse
import csv
import os
import random
import threading
import time

import numpy as np
import psycopg2
from qdrant_client import QdrantClient, models

PG = dict(host="localhost", port=5432, user="rag", password="ragpass", dbname="ragstudy")
QDRANT_URL = "http://localhost:6333"

FETCH = 20      # chunks the index returns per search
CTX_K = 5       # chunks that are put into the LLM context

TRIAL_HEADER = ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"]
ATTEMPT_HEADER = ["trial_id", "attempt_ts", "protected_returned", "protected_in_prompt",
                  "http_status", "latency_ms", "ref_returned", "ref_in_prompt"]


def doc_filter(doc_id):
    return models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])


# ------------------------------------------------------------------ search helpers (shared)
def build_context(hits, k_ctx=CTX_K):
    """Context builder with no gate: the top k_ctx chunks the index returned."""
    return hits[:k_ctx]


def load_pool(client, collection, n=50):
    """A random sample of chunk vectors, used to make the workload queries."""
    total = client.count(collection).count
    ids = random.sample(range(total), min(n, total))
    return [np.array(p.vector) for p in client.retrieve(collection, ids=ids, with_vectors=True)]


def query_vector(base, pool, min_alpha):
    """A question only partly about the document: alpha*doc + (1-alpha)*some other chunk."""
    alpha = random.uniform(min_alpha, 1.0)
    q = alpha * np.array(base) + (1 - alpha) * random.choice(pool)
    return (q / np.linalg.norm(q)).tolist()


def make_prober(client, collection, flt, doc, vec, pool, min_alpha, context_fn, on_error=None):
    """Returns probe(vary=True) -> (t, w_got, w_ctx, r_got, r_ctx, status, ms).
    w_* = workload search, r_* = reference search."""

    def flags(hits):
        got = int(any(h.payload["doc_id"] == doc for h in hits))
        ctx = int(any(h.payload["doc_id"] == doc for h in context_fn(hits)))
        return got, ctx

    def probe(vary=True):
        t, p0 = time.time(), time.perf_counter()
        try:
            ref = client.query_points(collection, query=vec, query_filter=flt, limit=FETCH).points
            r_got, r_ctx = flags(ref)
            q = query_vector(vec, pool, min_alpha) if vary else vec
            work = client.query_points(collection, query=q, query_filter=flt, limit=FETCH).points
            w_got, w_ctx = flags(work)
            status = 200
        except Exception:
            if on_error:
                on_error()
            w_got = w_ctx = r_got = r_ctx = 0
            status = 500
        return t, w_got, w_ctx, r_got, r_ctx, status, (time.perf_counter() - p0) * 1000

    return probe


def attempt_row(trial_id, p):
    t, w_got, w_ctx, r_got, r_ctx, status, ms = p
    return [trial_id, t, w_got, w_ctx, status, round(ms, 1), r_got, r_ctx]


def final_blocked_run(attempts):
    """Start index of the final run of reference probes that no longer reach the context."""
    i = len(attempts)
    while i > 0 and attempts[i - 1][7] == 0 and attempts[i - 1][4] == 200:
        i -= 1
    return i, (len(attempts) - i) >= 3


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
    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    next_run = time.monotonic() + interval
    while not stop.wait(max(0.0, next_run - time.monotonic())):
        sync_once(conn, client, collection)
        next_run += interval
    conn.close()


# ------------------------------------------------------------------ other helpers
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
def run_trial(a, conn, client, pool):
    trial_id = f"periodic-{a.interval}s-{count_rows(f'{a.out}/trials.csv') + 1}"
    doc, user = pick_pair(conn)
    flt = models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])
    vec = client.scroll(a.collection, scroll_filter=doc_filter(doc), limit=1,
                        with_vectors=True)[0][0].vector
    probe = make_prober(client, a.collection, flt, doc, vec, pool, a.min_alpha, build_context)

    _, _, _, r_got, r_ctx, status, _ = probe(vary=False)
    if not (r_ctx == 1 and status == 200):                    # baseline failed
        append_rows(f"{a.out}/trials.csv", TRIAL_HEADER,
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

    window = a.window or (a.interval + 15)
    attempts = []
    while time.time() < t_rev + window:
        p = probe()
        attempts.append(attempt_row(trial_id, p))
        time.sleep(max(0.0, 0.1 - p[6] / 1000))
    i, stable = final_blocked_run(attempts)

    stop.set()
    th.join()
    with conn.cursor() as cur:                                # restore for the next trial
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "VALUES (%s, 'user', %s)", (doc, user))
        cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
    conn.commit()
    sync_once(conn, client, a.collection)

    append_rows(f"{a.out}/trials.csv", TRIAL_HEADER,
                [[trial_id, "periodic", f"{a.interval}s", t_rev, 1, "ok"]])
    append_rows(f"{a.out}/attempts.csv", ATTEMPT_HEADER, attempts)

    urr = sum(x[2] for x in attempts) / len(attempts)
    cer = sum(x[3] for x in attempts) / len(attempts)
    if stable:
        print(f"{trial_id}: {user} / {doc}  RPT = {attempts[i][1] - t_rev:.2f} s   "
              f"URR = {urr:.2f}   CER = {cer:.2f}   ({len(attempts)} probes in a {window}s window)")
    else:
        print(f"{trial_id}: {user} / {doc}  NOT blocked within the window (censored)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30, help="sync interval in seconds")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--window", type=int, default=0,
                    help="observation window in seconds after revocation (default: interval + 15)")
    ap.add_argument("--min-alpha", type=float, default=0.3,
                    help="lowest relevance of a workload search to the protected document (0-1)")
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    with conn.cursor() as cur:                                # one-time, safe to repeat
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_dirty boolean DEFAULT false")
    conn.commit()
    pool = load_pool(client, a.collection)
    for _ in range(a.trials):
        run_trial(a, conn, client, pool)
    conn.close()


if __name__ == "__main__":
    main()
