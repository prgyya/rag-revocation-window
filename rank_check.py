import argparse
import random

import numpy as np
from qdrant_client import QdrantClient

ap = argparse.ArgumentParser()
ap.add_argument("--collection", default="chunks_1k")
ap.add_argument("--samples", type=int, default=60, help="protected chunks to test")
ap.add_argument("--url", default="http://localhost:6333")
a = ap.parse_args()

client = QdrantClient(url=a.url)
total = client.count(a.collection).count
pts = client.retrieve(a.collection, ids=random.sample(range(total), a.samples), with_vectors=True)
pool = [np.array(p.vector) for p in client.retrieve(a.collection, ids=random.sample(range(total), 50),
                                                    with_vectors=True)]
print(f"{'alpha':>6} {'in top 5':>10} {'in top 20':>10}")
for alpha in [1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]:
    top5 = top20 = 0
    for p in pts:
        q = alpha * np.array(p.vector) + (1 - alpha) * random.choice(pool)
        hits = client.query_points(a.collection, query=(q / np.linalg.norm(q)).tolist(), limit=20).points
        ranks = [i for i, h in enumerate(hits) if h.payload["doc_id"] == p.payload["doc_id"]]
        top5 += bool(ranks and ranks[0] < 5)
        top20 += bool(ranks)
    print(f"{alpha:>6} {top5 / len(pts):>10.0%} {top20 / len(pts):>10.0%}")
print("\nChoose --min-alpha where 'in top 20' is still high but 'in top 5' has dropped.")
