set -eo pipefail
python trial_periodic.py --interval 10 --window 25 --trials 10 --min-alpha 0.2 --p-relevant 0.5 --out results_test_v4 2>&1 | tee paper/test_v4_periodic.txt
python check_unrelated.py results_test_v4/attempts.csv | tee paper/check_unrelated.txt
python clean_logs.py --trials results_test_v4/trials.csv --attempts results_test_v4/attempts.csv --out results_test_v4_clean --warmup 0 | tee paper/test_v4_summary.txt