import argparse
import csv
import os
import random
import threading
import time
import uuid

import numpy as np
import psycopg2
from qdrant_client import QdrantClient, models

PG = dict(host="localhost", port=5432, user="rag", password="ragpass", dbname="ragstudy")
QDRANT_URL = "http://localhost:6333"

FETCH = 20      # chunks the index returns per search
CTX_K = 5       # chunks that are put into the LLM context

TRIAL_HEADER = ["trial_id", "mode", "config", "t_revocation", "baseline_ok", "status"]
ATTEMPT_HEADER = ["trial_id", "attempt_ts", "protected_returned", "protected_in_prompt",
                  "http_status", "latency_ms", "ref_returned", "ref_in_prompt",
                  "ctx_chunks", "ctx_chars", "relevant"]


def doc_filter(doc_id):
    return models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])


# ------------------------------------------------------------------ setup shared by all modes
def ensure_schema(conn):
    """One-time, safe to repeat. Every trial script calls this, so the column exists
    whichever mode runs first after setup_testbed.sh."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_dirty boolean DEFAULT false")
    conn.commit()


def seed_everything(conn, seed):
    """Python, NumPy and Postgres (pick_pair uses ORDER BY random()) all get the seed."""
    random.seed(seed)
    np.random.seed(seed)
    with conn.cursor() as cur:
        cur.execute("SELECT setseed(%s)", ((seed % 1000) / 1000.0,))
    conn.commit()


def new_trial_id(prefix, out):
    """Readable counter plus a short random suffix, so IDs never collide even if
    trials.csv is deleted or moved while attempts.csv is kept."""
    return f"{prefix}-{count_rows(f'{out}/trials.csv') + 1}-{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------ search helpers (shared)
def build_context(hits, k_ctx=CTX_K):
    """Context builder with no gate: the top k_ctx chunks the index returned."""
    return hits[:k_ctx]


def load_pool(client, collection, n=50):
    """A random sample of (chunk vector, doc_id), used to make the workload queries.
    Relies on embed_to_qdrant.py giving points the IDs 0 .. total-1."""
    total = client.count(collection).count
    ids = random.sample(range(total), min(n, total))
    pts = client.retrieve(collection, ids=ids, with_vectors=True, with_payload=["doc_id"])
    return [(np.array(p.vector), p.payload["doc_id"]) for p in pts]


def other_chunk(pool, doc):
    """A pool vector from a DIFFERENT document, so mixing really lowers relevance."""
    return random.choice([v for v, d in pool if d != doc])


def query_vector(base, pool, min_alpha, doc):
    """A question only partly about the document: alpha*doc + (1-alpha)*a chunk of another doc."""
    alpha = random.uniform(min_alpha, 1.0)
    q = alpha * np.array(base) + (1 - alpha) * other_chunk(pool, doc)
    return (q / np.linalg.norm(q)).tolist()


class ProbeResult(tuple):
    """(t, w_got, w_ctx, r_got, r_ctx, status, ms) plus how much unauthorized data the
    workload search put into the context: .n chunks of the protected document, .chars of text."""
    def __new__(cls, base, n=0, chars=0, relevant=1):
        obj = super().__new__(cls, base)
        obj.n, obj.chars, obj.relevant = n, chars, relevant
        return obj


def make_prober(client, collection, flt, doc, vec, pool, min_alpha, context_fn, on_error=None, p_relevant=1.0):
    """Returns probe(vary=True) -> (t, w_got, w_ctx, r_got, r_ctx, status, ms).
    w_* = workload search, r_* = reference search.

    A real user's questions are mostly NOT about the revoked document. So with probability
    p_relevant the workload search is about it (relevance drawn from min_alpha..1), and
    otherwise it is an unrelated question (a chunk of another document). `relevant` records which."""

    def flags(hits):
        got = int(any(h.payload["doc_id"] == doc for h in hits))
        mine = [h for h in context_fn(hits) if h.payload["doc_id"] == doc]
        return got, int(bool(mine)), len(mine), sum(len(h.payload.get("text", "")) for h in mine)

    def probe(vary=True):
        t, p0 = time.time(), time.perf_counter()
        relevant = 1
        try:
            ref = client.query_points(collection, query=vec, query_filter=flt, limit=FETCH).points
            r_got, r_ctx, _, _ = flags(ref)
            if not vary:
                q, relevant = vec, 1
            elif random.random() < p_relevant:
                q, relevant = query_vector(vec, pool, min_alpha, doc), 1
            else:
                q, relevant = other_chunk(pool, doc).tolist(), 0
            work = client.query_points(collection, query=q, query_filter=flt, limit=FETCH).points
            w_got, w_ctx, w_n, w_chars = flags(work)
            status = 200
        except Exception:
            if on_error:
                on_error()
            w_got = w_ctx = r_got = r_ctx = w_n = w_chars = 0
            status = 500
        return ProbeResult((t, w_got, w_ctx, r_got, r_ctx, status, (time.perf_counter() - p0) * 1000),
                           w_n, w_chars, relevant)

    return probe


def attempt_row(trial_id, p):
    t, w_got, w_ctx, r_got, r_ctx, status, ms = p
    return [trial_id, t, w_got, w_ctx, status, round(ms, 1), r_got, r_ctx, p.n, p.chars, p.relevant]


def final_blocked_run(attempts):
    """Start index of the final run of reference probes that no longer reach the context."""
    i = len(attempts)
    while i > 0 and attempts[i - 1][7] == 0 and attempts[i - 1][4] == 200:
        i -= 1
    return i, (len(attempts) - i) >= 3


def observe(trial_id, probe, t_rev, window, probe_ms):
    """Probe from the revocation until t_rev + window."""
    attempts = []
    while time.time() < t_rev + window:
        p = probe()
        attempts.append(attempt_row(trial_id, p))
        time.sleep(max(0.0, probe_ms / 1000 - p[6] / 1000))
    return attempts


def print_trial(trial_id, user, doc, attempts, t_rev, ms=False):
    """Console summary only. clean_logs.py computes the real figures from the CSVs."""
    i, stable = final_blocked_run(attempts)
    pre = attempts[:i] if stable else attempts
    cer = sum(x[3] for x in pre) / len(pre) if pre else 0.0
    urr = sum(x[2] for x in attempts) / len(attempts) if attempts else 0.0
    if stable:
        rpt = attempts[i][1] - t_rev
        rpt_s = f"{rpt * 1000:.0f} ms" if ms else f"{rpt:.2f} s"
        print(f"{trial_id}: {user} / {doc}  RPT = {rpt_s}   CER until blocked = {cer:.3f}   "
              f"URR whole window = {urr:.3f}   ({len(attempts)} probes)")
    else:
        print(f"{trial_id}: {user} / {doc}  NOT blocked within the window (censored)   "
              f"CER = {cer:.3f}   URR = {urr:.3f}")


# ------------------------------------------------------------------ revoke / restore (shared)
def revoke_grant(conn, doc, user, mark_dirty=True, notify=False):
    """Delete the user's direct grant in the authoritative store. Optionally mark the
    document dirty (durable record for a sync job) and publish an event; both happen in
    the same transaction, so nothing is visible before the revocation commits."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM doc_acl WHERE doc_id = %s AND principal_type = 'user' "
                    "AND principal_id = %s", (doc, user))
        if mark_dirty:
            cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
        if notify:
            cur.execute("SELECT pg_notify('acl_changed', %s)", (doc,))
    conn.commit()
    return time.time()


def restore_grant(conn, doc, user, mark_dirty=True):
    """Put the grant back. Idempotent, so it is safe to run after a crash at any point."""
    conn.rollback()                                   # clear a transaction a crash may have left open
    with conn.cursor() as cur:
        cur.execute("INSERT INTO doc_acl(doc_id, principal_type, principal_id) "
                    "SELECT %s, 'user', %s WHERE NOT EXISTS (SELECT 1 FROM doc_acl "
                    "WHERE doc_id = %s AND principal_type = 'user' AND principal_id = %s)",
                    (doc, user, doc, user))
        if mark_dirty:
            cur.execute("UPDATE documents SET acl_dirty = true WHERE doc_id = %s", (doc,))
    conn.commit()


def save_trial(out, trial_id, mode, config, t_rev, status, attempts):
    append_rows(f"{out}/trials.csv", TRIAL_HEADER, [[trial_id, mode, config, t_rev, 1, status]])
    append_rows(f"{out}/attempts.csv", ATTEMPT_HEADER, attempts)


def save_baseline_failure(out, trial_id, mode, config, user, doc):
    append_rows(f"{out}/trials.csv", TRIAL_HEADER,
                [[trial_id, mode, config, time.time(), 0, "baseline_failed"]])
    print(f"{trial_id}: baseline failed for {user} / {doc}, trial skipped")


# ------------------------------------------------------------------ the sync layer
def sync_once(conn, client, collection):
    """Copy the access list of every changed document from Postgres into Qdrant.
    The dirty flags are cleared FIRST (in the same transaction), so a revocation that
    lands while we read sets the flag again and is picked up by the next run."""
    with conn.cursor() as cur:
        cur.execute("UPDATE documents SET acl_dirty = false WHERE acl_dirty RETURNING doc_id")
        for (d,) in cur.fetchall():
            cur.execute("SELECT principal_type, principal_id FROM doc_acl WHERE doc_id = %s", (d,))
            principals = [f"{t}:{p}" for t, p in cur.fetchall()]
            client.set_payload(collection, payload={"allowed_principals": principals},
                               points=doc_filter(d))
    conn.commit()            # if set_payload failed we never get here, and the flags stay set


def sync_loop(interval, stop, collection):
    conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
    next_run = time.monotonic() + interval
    while not stop.wait(max(0.0, next_run - time.monotonic())):
        try:
            sync_once(conn, client, collection)
        except Exception as e:                       # log and retry next tick; never die silently
            print(f"[sync] error: {e!r}", flush=True)
            try:
                conn.rollback()
            except Exception:
                conn = psycopg2.connect(**PG)
        next_run += interval
    conn.close()


# ------------------------------------------------------------------ other helpers
def pick_pair(conn):
    """A direct user grant that is the user's ONLY path to the document (no public row,
    no group the user belongs to), so deleting it really revokes access."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.doc_id, a.principal_id FROM doc_acl a
            WHERE a.principal_type = 'user'
              AND NOT EXISTS (
                SELECT 1 FROM doc_acl b
                WHERE b.doc_id = a.doc_id
                  AND (b.principal_type = 'public'
                       OR (b.principal_type = 'group' AND b.principal_id IN
                           (SELECT group_id FROM group_members WHERE user_id = a.principal_id))))
            ORDER BY random() LIMIT 1""")
        return cur.fetchone()


def principals_for(conn, user):
    with conn.cursor() as cur:
        cur.execute("SELECT group_id FROM group_members WHERE user_id = %s", (user,))
        groups = [r[0] for r in cur.fetchall()]
    return ["public:*", f"user:{user}"] + [f"group:{g}" for g in groups]


def user_filter(conn, user):
    return models.Filter(must=[models.FieldCondition(
        key="allowed_principals", match=models.MatchAny(any=principals_for(conn, user)))])


def doc_vector(client, collection, doc):
    return client.scroll(collection, scroll_filter=doc_filter(doc), limit=1,
                         with_vectors=True)[0][0].vector


def append_rows(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def count_rows(path):
    return sum(1 for _ in open(path)) - 1 if os.path.exists(path) else 0


def add_common_args(ap, out_default):
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--min-alpha", type=float, default=0.3,
                    help="lowest relevance of a workload search to the protected document (0-1)")
    ap.add_argument("--p-relevant", type=float, default=0.5,
                    help="share of workload searches that are about the protected document (rest are unrelated)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--out", default=out_default)


# ------------------------------------------------------------------ one trial
def run_trial(a, conn, client, pool):
    trial_id = new_trial_id(f"periodic-{a.interval}s", a.out)
    config = f"{a.interval}s"
    doc, user = pick_pair(conn)
    flt = user_filter(conn, user)
    probe = make_prober(client, a.collection, flt, doc, doc_vector(client, a.collection, doc),
                        pool, a.min_alpha, build_context, p_relevant=a.p_relevant)

    _, _, _, r_got, r_ctx, status, _ = probe(vary=False)
    if not (r_ctx == 1 and status == 200):
        save_baseline_failure(a.out, trial_id, "periodic", config, user, doc)
        return

    stop = threading.Event()
    th = threading.Thread(target=sync_loop, args=(a.interval, stop, a.collection), daemon=True)
    th.start()
    t_rev, attempts, result = None, [], "crashed"
    try:
        time.sleep(random.uniform(0, a.interval))             # revoke at a random point in the cycle
        t_rev = revoke_grant(conn, doc, user, mark_dirty=True)  # T_revocation
        attempts = observe(trial_id, probe, t_rev, a.window or (a.interval + 15), a.probe_ms)
        result = "ok"
    finally:                                                  # always restore, even after Ctrl+C or a crash
        stop.set()
        th.join()
        restore_grant(conn, doc, user, mark_dirty=True)
        sync_once(conn, client, a.collection)
        if t_rev is not None:
            save_trial(a.out, trial_id, "periodic", config, t_rev, result, attempts)
    print_trial(trial_id, user, doc, attempts, t_rev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30, help="sync interval in seconds")
    ap.add_argument("--window", type=int, default=0,
                    help="observation window in seconds after revocation (default: interval + 15)")
    ap.add_argument("--probe-ms", type=int, default=100, help="target gap between probes")
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
