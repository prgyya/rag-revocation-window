#!/usr/bin/env bash
set -euo pipefail

wait_for() {   # wait_for "<description>" <command...>; gives up after 2 minutes
  local what=$1; shift
  for _ in $(seq 60); do "$@" >/dev/null 2>&1 && return 0; sleep 2; done
  echo "ERROR: $what did not become ready within 2 minutes" >&2; exit 1
}

echo "== 1/7 installing Python libraries"
if [ -f requirements.lock ]; then pip install -r requirements.lock; else pip install -r requirements.txt; fi

echo "== 2/7 starting Postgres and Qdrant"
docker compose up -d
wait_for "Postgres" docker exec rag_postgres pg_isready -h 127.0.0.1 -U rag -d ragstudy
wait_for "Qdrant" curl -sf localhost:6333/collections

echo "== 3/7 generating and cleaning the dataset (seed 42, so everyone gets the same data)"
[ -f data_1k/schema.sql ] || python build_dataset.py --users 1000 --docs 2500 --source synthetic \
                                    --scenario medium --seed 42 --out data_1k
python clean_data.py --in data_1k --out data_1k_clean
cp data_1k/schema.sql data_1k_clean/

echo "== 4/7 loading permissions into Postgres"
docker exec rag_postgres psql -v ON_ERROR_STOP=1 -U rag -d ragstudy -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker exec rag_postgres rm -rf /tmp/data
docker cp data_1k_clean/. rag_postgres:/tmp/data
docker exec -w /tmp/data rag_postgres psql -v ON_ERROR_STOP=1 -U rag -d ragstudy -f schema.sql

echo "== 5/7 embedding the chunks and loading them into Qdrant"
python embed_to_qdrant.py --in data_1k_clean --collection chunks_1k

echo "== 6/7 counts (expect users 1000, docs 2500, acl_rows 3501)"
docker exec rag_postgres psql -v ON_ERROR_STOP=1 -U rag -d ragstudy -c "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM documents) AS docs, (SELECT count(*) FROM doc_acl) AS acl_rows;"

echo "== 7/7 checking that Qdrant and Postgres agree"
python verify_testbed.py --collection chunks_1k
echo "Setup finished."
