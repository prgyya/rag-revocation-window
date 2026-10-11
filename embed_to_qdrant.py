import argparse
import json
import time

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

VECTOR_SIZE = 384            # output size of all-MiniLM-L6-v2


def read_chunks(path):
    with open(path) as f:
        for line in f:
            yield json.loads(line)


def batches(items, size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="cleaned data folder")
    ap.add_argument("--collection", default="chunks")
    ap.add_argument("--model", default="all-MiniLM-L6-v2")
    ap.add_argument("--url", default="http://localhost:6333")
    ap.add_argument("--batch", type=int, default=128)
    a = ap.parse_args()

    client = QdrantClient(url=a.url)
    model = SentenceTransformer(a.model)

    # start from an empty collection so reruns do not mix old and new data
    if client.collection_exists(a.collection):
        client.delete_collection(a.collection)
    client.create_collection(
        collection_name=a.collection,
        vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
    )
    # indexes make permission filters fast
    client.create_payload_index(a.collection, "allowed_principals", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(a.collection, "doc_id", models.PayloadSchemaType.KEYWORD)

    t0, next_id = time.time(), 0
    for batch in batches(read_chunks(f"{a.inp}/chunks.jsonl"), a.batch):
        vectors = model.encode([c["text"] for c in batch], batch_size=64,
                               normalize_embeddings=True, show_progress_bar=False)
        points = []
        for c, v in zip(batch, vectors):
            points.append(models.PointStruct(
                id=next_id, vector=v.tolist(),
                payload={"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "text": c["text"],
                         "allowed_principals": c["allowed_principals"],
                         "acl_version": c.get("acl_version", 1)}))
            next_id += 1
        client.upsert(collection_name=a.collection, points=points)
        print(f"uploaded {next_id} chunks  ({time.time() - t0:.0f}s)")

    print(f"\nQdrant now holds {client.count(a.collection).count} points in '{a.collection}'")

    # quick sanity search (no permission filter yet)
    q = model.encode("quarterly budget review", normalize_embeddings=True).tolist()
    for hit in client.query_points(a.collection, query=q, limit=3).points:
        print(f"  score={hit.score:.3f}  {hit.payload['chunk_id']}  {hit.payload['allowed_principals']}")


if __name__ == "__main__":
    main()