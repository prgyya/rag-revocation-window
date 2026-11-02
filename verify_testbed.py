import argparse
import sys

import psycopg2
from qdrant_client import QdrantClient

from trial_periodic import PG, QDRANT_URL

ap = argparse.ArgumentParser()
ap.add_argument("--collection", default="chunks_1k")
a = ap.parse_args()

conn, client = psycopg2.connect(**PG), QdrantClient(url=QDRANT_URL)
with conn.cursor() as cur:
    cur.execute("SELECT doc_id, principal_type, principal_id FROM doc_acl")
    want = {}
    for d, t, p in cur.fetchall():
        want.setdefault(d, set()).add(f"{t}:{p}")
    cur.execute("""SELECT count(*) FROM doc_acl a WHERE a.principal_type = 'user' AND NOT EXISTS (
                     SELECT 1 FROM doc_acl b WHERE b.doc_id = a.doc_id AND (b.principal_type = 'public'
                       OR (b.principal_type = 'group' AND b.principal_id IN
                           (SELECT group_id FROM group_members WHERE user_id = a.principal_id))))""")
    pairs = cur.fetchone()[0]

mismatch, seen, offset, points = [], set(), None, 0
while True:
    pts, offset = client.scroll(a.collection, limit=1000, offset=offset,
                                with_payload=["doc_id", "allowed_principals"])
    for p in pts:
        points += 1
        d = p.payload["doc_id"]
        seen.add(d)
        if set(p.payload["allowed_principals"]) != want.get(d, set()):
            mismatch.append(d)
    if offset is None:
        break

info = client.get_collection(a.collection)
indexed = info.indexed_vectors_count or 0
print(f"Qdrant points: {points}   documents with points: {len(seen)}   documents in Postgres ACL: {len(want)}")
print(f"ACL mismatches (Qdrant vs Postgres): {len(set(mismatch))} documents")
print(f"HNSW index built: {'yes' if indexed >= points else 'no'} ({indexed} of {points} vectors indexed)"
      + ("" if indexed >= points else " -> searches are exact (brute force); say so in the report"))
print(f"eligible revocation pairs for pick_pair: {pairs}")

ok = not mismatch and seen == set(want)
print("testbed consistent" if ok else "TESTBED INCONSISTENT: re-run setup_testbed.sh")
sys.exit(0 if ok else 1)
