# Revocation Window in RAG

Measures how long a user whose access has been revoked can still retrieve protected content
from a RAG system, and how much protected content leaks during that time.

## Metrics

| Name | Meaning |
|---|---|
| **RPT** (Revocation Propagation Time) | `t_enforcement - t_revocation`. Enforcement is the first probe of the final run of at least 3 consecutive blocked probes that stays blocked to the end of the trial. Reported twice: `rpt_index_s` (the index stops returning the chunk) and `rpt_ctx_s` (the chunk stops reaching the LLM context). In the summary these appear as `idx_*` and `ctx_*` columns. RPT is measured from a clean reference search. |
| **URR** (Unauthorized Retrieval Rate) | Share of post-revocation searches in the fixed window where the index returned the protected chunk. Measured from a noisier workload search. Depends on the window length. |
| **CER** (Context Exposure Rate) | Share of post-revocation searches in the fixed window where the chunk reached the LLM context. Depends on the window length. |
| `urr_interval`, `cer_interval` | The same two rates, counted only between revocation and enforcement (the interval ends at the context enforcement time), so they do not shrink when the window is longer. `NaN` means no probe fell inside the interval, which is the normal result in the gate modes. |
| `leaked_probes` | Number of probes in that interval that returned the protected chunk (mean per trial in the summary). It is a count of probes, so it depends on the probe rate. Compare it only between runs with the same settings. |
| `ctx_chunks` | Chunks of the protected document in the LLM context per probe, over the same interval. |
| `ctx_chars` | Characters of protected text in the LLM context per probe, over the same interval. |
| `exp_exposures` | Expected number of times protected text reaches the context for a revoked user who issues `--qpm` queries per minute (an assumed rate, default 1): `qpm / 60 x rpt_ctx_s x cer_interval`. |
| `lat_mean`, `lat_p95` | Probe latency inside the window. One probe is two searches (plus the gate queries in gate modes). In the summary, `lat_p95` is the mean of each trial's p95. |
| Censored | A trial never stably blocked in the window. Counted, never deleted. |

Compare URR and CER only between runs that used the same `--window`.

## Enforcement modes compared

- **periodic**: a sync job copies changed access lists from Postgres into Qdrant every `--interval` seconds
- **event**: Postgres `LISTEN/NOTIFY`; a worker updates Qdrant when a permission changes
- **query-time**: no index filter; a fresh Postgres check decides what reaches the context
- **hybrid**: the (stale) index filter pre-filters, then the Postgres check decides
- **control**: the event run with the worker switched off (`--no-worker`); the user should never be blocked

In query-time and hybrid the index is deliberately not updated, so the gate alone enforces the revocation.

## Setup (GitHub Codespaces)

1. Create a codespace on `main`.
2. Run `git pull` and `bash setup_testbed.sh` (about 10 minutes).
3. Expected final check: users 1000, docs 2500, acl_rows 3501.

The generated data is not stored in the repository. It is rebuilt from a fixed seed (42).

## Running experiments

Settings used for every run: sync interval 20 s, one observation window of 35 s for all modes
(so URR and CER are comparable across rows), and one `--min-alpha` value.

1. Choose the search-relevance setting:

```
python rank_check.py --collection chunks_1k
```

Pick the `--min-alpha` where "in top 20" is still high but "in top 5" has dropped. Then run a short
event test and confirm that CER is below URR and every `http_status` is 200. Use the same value below
(`<ALPHA>`).

2. Run the experiments one after another inside one Codespace, because every trial changes the same
database. Separate Codespaces (for example one per teammate) each have their own database and can run
different modes in parallel:

```
python trial_periodic.py --interval 20 --window 35 --trials 30 --min-alpha <ALPHA> --out results_periodic_v3
python trial_event.py --window 35 --trials 30 --min-alpha <ALPHA> --out results_event_v3
python trial_event.py --window 35 --trials 5 --no-worker --min-alpha <ALPHA> --out results_event_v3
python trial_gate.py --mode query-time --window 35 --trials 30 --min-alpha <ALPHA> --out results_gate_v3
python trial_gate.py --mode hybrid --window 35 --trials 30 --min-alpha <ALPHA> --out results_gate_v3
```

3. Clean each folder:

```
python clean_logs.py --trials results_X/trials.csv --attempts results_X/attempts.csv --out results_X_clean --warmup 3 --window 35
```

The first 3 trials of each mode and config are dropped as warm-up. The `no-worker` control is exempt.
Add `--qpm <N>` to set the assumed queries per minute used for `exp_exposures`.

## Earlier pilot results

The folders `results_periodic`, `results_event` and `results_gate` (with their `_clean` versions) come from the
first version of the scripts: 20 s interval, and CER recorded as a copy of URR in periodic and event modes. They are
a pilot and should not be mixed with the `_v3` folders.

## Files

- `build_dataset.py`, `clean_data.py`: generate and clean the synthetic dataset
- `embed_to_qdrant.py`: embed chunks and load them into Qdrant
- `search_as.py`: permission-filtered search as one user
- `trial_periodic.py`: periodic-sync trials, and the helpers shared by the other trial scripts
- `trial_event.py`: event-driven trials and the no-worker control
- `trial_gate.py`: query-time and hybrid trials
- `clean_logs.py`: cleans trial logs and computes RPT, URR, CER and the interval measures
- `rank_check.py`: helps choose the search-relevance setting (`--min-alpha`)
- `setup_testbed.sh`, `requirements.txt`: one-command setup for a fresh Codespace

## Team

Pragya Rai, Chetna Umakant Shete, Omkar Sanjay Jadhav, Swaraj Santosh Godalkar
