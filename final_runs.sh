set -eo pipefail
A="--window 35 --min-alpha 0.2 --p-relevant 0.5"
python trial_periodic.py --interval 20 --trials 30 $A --out results_periodic_v3 2>&1 | tee paper/run_periodic.txt
python trial_event.py --trials 30 $A --out results_event_v3 2>&1 | tee paper/run_event.txt
python trial_event.py --trials 5 --no-worker $A --out results_event_v3 2>&1 | tee paper/run_noworker.txt
python trial_gate.py --mode query-time --trials 30 $A --out results_gate_v3 2>&1 | tee paper/run_querytime.txt
python trial_gate.py --mode hybrid --trials 30 $A --out results_gate_v3 2>&1 | tee paper/run_hybrid.txt
python trial_gate.py --mode query-time --cache-ttl 10 --trials 30 $A --out results_gate_v3 2>&1 | tee paper/run_cache.txt
echo "ALL RUNS FINISHED"