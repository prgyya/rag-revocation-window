set -eo pipefail
echo "date: $(date)" > paper/environment.txt
echo "commit: $(git log -1 --oneline)" >> paper/environment.txt
python --version >> paper/environment.txt
echo "machine: $(nproc) cores, $(free -h | awk '/Mem/{print $2}') RAM" >> paper/environment.txt
docker exec rag_postgres postgres --version >> paper/environment.txt
curl -s localhost:6333 | python -c "import sys,json; print('qdrant', json.load(sys.stdin)['version'])" >> paper/environment.txt
pip freeze > requirements.lock
cp data_1k_clean/cleaning_report.csv paper/
cat paper/environment.txt