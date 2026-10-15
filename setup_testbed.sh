#!/usr/bin/env bash
set -e

echo "== 1/6 installing Python libraries"
pip install -r requirements.txt

echo "== 2/6 starting Postgres and Qdrant"
docker compose up -d
until docker exec rag_postgres pg_isready -U rag -d ragstudy >/dev/null 2>&1; do sleep 2; done
until curl -s localhost:6333/collections >/dev/null 2>&1; do sleep 2; done

echo "== 3/6 generating and cleaning the dataset (fixed seed, so everyone gets the same data)"
[ -d data_1k ] || python build_dataset.py --users 1000 --docs 2500 --source synthetic --scenario medium --out data_1k
python clean_data.py --in data_1k --out data_1k_clean
cp data_1k/schema.sql data_1k_clean/

echo "== 4/6 loading permissions into Postgres"
docker exec rag_postgres psql -U rag -d ragstudy -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker exec rag_postgres rm -rf /tmp/data
docker cp data_1k_clean/. rag_postgres:/tmp/data
docker exec -w /tmp/data rag_postgres psql -U rag -d ragstudy -f schema.sql

echo "== 5/6 embedding the chunks and loading them into Qdrant"
python embed_to_qdrant.py --in data_1k_clean --collection chunks_1k

echo "== 6/6 checking the result (expect users 1000, docs 2500, acl_rows 3501)"
docker exec rag_postgres psql -U rag -d ragstudy -c "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM documents) AS docs, (SELECT count(*) FROM doc_acl) AS acl_rows;"
echo "Setup finished."
