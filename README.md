# Revocation Window in RAG

Measures how long a user whose access has been revoked can still retrieve protected content
from a RAG system, and how much protected content leaks during that time.

## Metrics

| Name | Meaning |
|---|---|
| **RPT** (Revocation Propagation Time) | `t_enforcement - t_revocation`. Enforcement is the first probe of the final run of at least 3 consecutive blocked probes that stays blocked to the end of the trial. Reported twice: `rpt_index_s` (the index stops returning the chunk) and `rpt_ctx_s` (the chunk stops reaching the LLM context). In the summary these appear as `idx_*` and `ctx_*` columns. RPT is measured from a clean reference search. |
| **URR** (Unauthorized Retrieval Rate) | Share of post-revocation searches in the fixed window where the index returned the protected chunk. Measured from a noisier workload search. Depends on the window length. |
| **CER** (Context Exposure Rate) | Share of post-revocation searches in the fixed window where the chunk reached the LLM context. Depends on the window length. |
| `urr_interval`, `cer_interval` | The same two rates, counted only between revocation and enforcement, so they do not shrink when the window is longer. The URR interval ends when the **index** stops returning the chunk (never, in the gate modes, so it covers the whole window and shows the stale index leaking); the CER interval ends when the chunk stops reaching the **context**. `NaN` means no probe fell inside the interval, which is the normal CER result in the gate modes. |
| `urr_rel_interval`, `cer_rel_interval` (`urr_rel`, `cer_rel` in the summary) | The same two rates, but only over the probes that were **about the protected document**, inside the interval. These are the headline exposure rates: they say how likely the chunk is to leak when the revoked user asks a question on that topic, and they do not depend on how often users ask such questions. |
| `relevant` | Raw-log column: 1 if the workload search was about the protected document, 0 if it was an unrelated question. The share is set by `--p-relevant`. |
| `leaked_probes` | Number of probes in the URR interval that returned the protected chunk (mean per trial in the summary). It is a count of probes, so it depends on the probe rate. Compare it only between runs with the same settings. |
| `ctx_chunks` | Chunks of the protected document in the LLM context per probe, over the same interval. |
| `ctx_chars` | Characters of protected text in the LLM context per probe, over the same interval. |
| `exp_exposures` | Expected number of times protected text reaches the context for a revoked user: `qpm / 60 x share_relevant x rpt_ctx_s x cer_rel_interval`. `--qpm` (queries per minute, default 1) and `--share-relevant` (share of those queries about the revoked document, default 0.1) are **assumptions**, not measurements. Vary them to show how the expected exposure scales. It is computed per mode from the mean `rpt_ctx_s` and the pooled `cer_rel`; if no relevant probe fell inside any interval, `cer_rel` is taken as 1 and `exp_upper_bound` is True (the value is then an upper bound). |
| `lat_mean`, `lat_p95` | Probe latency inside the window. One probe is two searches (plus the gate queries in gate modes). In the summary, `lat_p95` is the mean of each trial's p95. |
| Censored | A trial never stably blocked in the window. Counted, never deleted. |

Compare URR and CER only between runs that used the same `--window` and the same `--p-relevant`.

In `summary.csv` the interval rates (`urr_interval`, `cer_interval`, `urr_rel`, `cer_rel`, `ctx_chunks`, `ctx_chars`) are **pooled** over all probes of the mode, so a trial with a single probe does not weigh as much as one with hundreds. This matters in event mode, where the leak lasts only a few probes.

**How the workload is modelled.** The periodic probes run about ten times per second and the event and gate probes about a hundred, which is a measurement instrument and not a claim about user behaviour. Each probe is one random draw of "a question the revoked user might ask". With probability `--p-relevant` (default 0.5) the question is about the protected document, with relevance drawn between `--min-alpha` and 1; otherwise it is an unrelated question. The index returns 20 chunks (`FETCH`) and 5 go into the context (`CTX_K`). Real exposure counts follow from the conditional rates and an assumed query rate (see `exp_exposures`).

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
(so URR and CER are comparable across rows), `--min-alpha 0.2` and `--p-relevant 0.5`.

1. Choose the search-relevance setting:

```
python rank_check.py --collection chunks_1k
```

On our data it showed 100% in the top 5 and top 20 down to alpha 0.5, 92% / 98% at 0.4, and 43% / 63% at 0.3,
so we use `--min-alpha 0.2`. Then run a short periodic test (the leak must stay open for seconds to see the
rates) and confirm that `urr_interval` is above `cer_interval`, `urr_rel` is above `urr_interval`, and every
`http_status` is 200:

```
python trial_periodic.py --interval 10 --window 25 --trials 3 --min-alpha 0.2 --p-relevant 0.5 --out results_test_v3b
python clean_logs.py --trials results_test_v3b/trials.csv --attempts results_test_v3b/attempts.csv --out results_test_v3b_clean --warmup 0 --window 25
```

Use a fresh output folder for every test. Logs written by older versions of the scripts have fewer columns, and appending to them corrupts the file.

2. Run the experiments one after another inside one Codespace, because every trial changes the same
database. Separate Codespaces (for example one per teammate) each have their own database and can run
different modes in parallel:

```
python trial_periodic.py --interval 20 --window 35 --trials 30 --min-alpha 0.2 --p-relevant 0.5 --out results_periodic_v3
python trial_event.py --window 35 --trials 30 --min-alpha 0.2 --p-relevant 0.5 --out results_event_v3
python trial_event.py --window 35 --trials 5 --no-worker --min-alpha 0.2 --p-relevant 0.5 --out results_event_v3
python trial_gate.py --mode query-time --window 35 --trials 30 --min-alpha 0.2 --p-relevant 0.5 --out results_gate_v3
python trial_gate.py --mode hybrid --window 35 --trials 30 --min-alpha 0.2 --p-relevant 0.5 --out results_gate_v3
```

3. Clean each folder:

```
python clean_logs.py --trials results_X/trials.csv --attempts results_X/attempts.csv --out results_X_clean --warmup 3 --window 35
```

The first 3 trials of each mode and config are dropped as warm-up. The `no-worker` control is exempt.
Add `--qpm <N>` and `--share-relevant <S>` to set the assumed query rate and the assumed share of relevant queries used for `exp_exposures`.

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
