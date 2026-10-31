# Revocation Window in RAG

Measures how long a user whose access has been revoked can still retrieve protected content
from a RAG system, and how much protected content leaks during that time.

## Metrics
- **RPT** (Revocation Propagation Time): time from revocation at the source until the
  protected chunk stops reaching the LLM context, confirmed by 3 consecutive blocked probes.
- **URR** (Unauthorized Retrieval Rate): share of searches where the index still returned the chunk.
- **CER** (Context Exposure Rate): share of searches where the chunk reached the LLM context.

## Enforcement modes compared
Periodic sync, event-driven sync (Postgres LISTEN/NOTIFY), query-time authorization, hybrid,
plus a control run with the event worker switched off.

## Setup (GitHub Codespaces)
1. Create a codespace on `main`.
2. Run `git pull` and `bash setup_testbed.sh` (about 10 minutes).
3. Expected final check: users 1000, docs 2500, acl_rows 3501.

The generated data is not stored in the repository. It is rebuilt from a fixed seed (42).

## Running experiments
```
python trial_periodic.py --interval 30 --window 45 --trials 30 --out results_periodic_v3
python trial_event.py --window 45 --trials 30 --out results_event_v3
python trial_gate.py --mode query-time --window 45 --trials 30 --out results_gate_v3
python clean_logs.py --trials results_X/trials.csv --attempts results_X/attempts.csv --out results_X_clean --window 45
```

## Files
- `build_dataset.py`, `clean_data.py`: generate and clean the synthetic dataset
- `embed_to_qdrant.py`: embed chunks and load them into Qdrant
- `search_as.py`: permission-filtered search as one user
- `trial_periodic.py`, `trial_event.py`, `trial_gate.py`: timed revocation trials
- `clean_logs.py`: cleans trial logs and computes RPT, URR, CER
- `rank_check.py`: helps choose the search-relevance setting

## Team
Pragya Rai,
Chetna Umakant Shete,
Omkar Sanjay Jadhav,
Swaraj Santosh Godalkar
