import argparse

import psycopg2
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer


def principals_for(user_id):
    """Everything this user may be matched against: public, themselves, their groups."""
    conn = psycopg2.connect(host="localhost", port=5432, user="rag",
                            password="ragpass", dbname="ragstudy")
    with conn, conn.cursor() as cur:
        cur.execute("SELECT group_id FROM group_members WHERE user_id = %s", (user_id,))
        groups = [row[0] for row in cur.fetchall()]
    conn.close()
    return ["public:*", f"user:{user_id}"] + [f"group:{g}" for g in groups]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--collection", default="chunks_1k")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-filter", action="store_true", help="ignore permissions (for comparison)")
    a = ap.parse_args()

    principals = principals_for(a.user)
    print(f"{a.user} may match: {principals}\n")

    model = SentenceTransformer("all-MiniLM-L6-v2")
    vector = model.encode(a.query, normalize_embeddings=True).tolist()

    flt = None
    if not a.no_filter:
        flt = models.Filter(must=[models.FieldCondition(
            key="allowed_principals", match=models.MatchAny(any=principals))])

    client = QdrantClient(url="http://localhost:6333")
    hits = client.query_points(a.collection, query=vector, query_filter=flt, limit=a.limit).points
    for h in hits:
        allowed = set(h.payload["allowed_principals"]) & set(principals)
        print(f"score={h.score:.3f}  {h.payload['chunk_id']}  list={h.payload['allowed_principals']}"
              f"  -> allowed by {sorted(allowed) if allowed else 'NOTHING (leak!)'}")


if __name__ == "__main__":
    main()