set -eo pipefail
for m in periodic event gate; do
  python clean_logs.py --trials results_${m}_v3/trials.csv --attempts results_${m}_v3/attempts.csv --out results_${m}_v3_clean --warmup 3 | tee paper/summary_${m}.txt
done
python make_tables.py results_periodic_v3_clean results_event_v3_clean results_gate_v3_clean --out report_tables | tee paper/final_table.txt