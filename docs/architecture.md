# Architecture (AI-oriented reference)

This document is organized as technical context for AI coding agents, not as a human-facing guide.

```text
experiments/* -> ls; show -> available experiments
              -> show <experiment> -> experiment details
              -> squeeze <experiment> [<scenario> [<candidate>]] -> selection details
              -> squeeze <experiment> [<scenario> [<candidate>]] --run [-s N]
                                          |
                            in-memory plan: selected measurements
                            optional --save-plan -> _juice/plan-<execution_id>.json
                                          |
                              Compose cleanup -> fixture, Pulp, candidate pre-test functions
                              Locust library: idle, load, tail, measurements
                              s6 collectors -> Parquet -> Compose down (or retain with --post-debug)
                                          |
                              _juice/executions/<execution_id>/ + RunResultMetadata
```

Each `squeeze --run` call is an execution, identified by `experiment[__scenario[__candidate]]__NNN` (for example, `bucket-repository__burst__001`). The final number increases across the workspace. Its plan contains the selected ordered measurements; each records `(experiment, scenario, run_id)` plus candidate, fixture, and load metadata. A scenario's `run_order` is a tuple of candidate IDs, optionally generated in the scenario declaration by calling `sample_sequence(n, candidates)`. The helper alternates forward and reverse candidate rounds. `-s N` replaces the configured order with N samples of every candidate. Draft scenarios are excluded from whole-experiment runs but can be selected explicitly. Pulp is reset before every measurement. Generated fixtures and results live under `_juice/`.

Scenario load profiles are `UnitaryImpulseLoad` for one request per user or `StepFunctionLoad` for timed changes to the target user count. Both keep request count and timeout settings with the load profile and derive Locust spawn rates internally.

| Scope | Command |
|:--|:--|
| List experiments and command hint | `pulp-juicer squeeze` |
| List candidates and scenarios | `pulp-juicer squeeze bucket-repository` |
| Inspect one scenario | `pulp-juicer squeeze bucket-repository burst` |
| Inspect one candidate | `pulp-juicer squeeze bucket-repository burst normal` |
| Experiment | `pulp-juicer squeeze bucket-repository --run` |
| Scenario | `pulp-juicer squeeze bucket-repository burst --run -s 3` |
| Ad-hoc candidate | `pulp-juicer squeeze bucket-repository burst normal --run` |

Each execution builds its plan in memory and reserves a result directory. `_juice/.execution-sequence` keeps execution numbers increasing across calls. `--save-plan` writes the selected inputs as JSON, omitting the Pulp password. `-f` replaces the latest execution for the selected scope. `_juice/executions/<execution_id>/execution.log` records plan preparation, fixture reuse, test hooks, load start and stop, and failures. `ls` lists experiments and scenarios with short descriptions; `show` lists experiments, while `show <experiment>` shows its full description, scenarios, candidates, and runs. `ps` lists pulp-juicer services. `clean <targets>` removes services, results, fixtures, or all three; targets can be comma separated, and saved plans remain. Completed runs retain results even when requests fail; incomplete runs retain only their Compose log.

`--post-debug` requires an ad-hoc candidate execution. It retains the Compose services after the run and prints inspection and cleanup commands; the status and Compose log are still captured.

Each successful run stores its execution ID, configuration, setup, source details, and result metrics in one `run_metadata.json`. Its selected requests and request measurements are written directly as `data_load.parquet` and `requests.parquet`; the full manifest stays under `_juice/fixture/`. After the load test and before service teardown, it saves the Pulp status API response as `pulp_status.json` in the unit result directory. The Pulp image installs an s6 collector RPM; `PulpComponents` discovers API, content, task worker, and PostgreSQL processes from s6 and reads Pulp's effective database and working-directory settings. Periodic collectors sample independently into Parquet files. Before teardown, the runner requests the on-demand collector and then finalizes periodic files over a Unix socket. After the daemon confirms their Parquet footers are written, the runner copies them into `collectors/` in the run results. Capture errors are saved alongside the run. Every run saves timestamped service output from Compose in `compose.log` before teardown, including failed runs; incomplete measurement files are removed while this log is kept.
Before teardown, the on-demand collector snapshots task rows from Pulp's PostgreSQL database into `collectors/tasking.parquet`. Each row includes task state and creation, unblock, start, and finish times; reports derive the two queue-delay phases and execution time from these timestamps. `compose.log` remains for debugging. Completed runs appear in `_juice/executions/<execution_id>/report.html` and the trial comparison `trials.html`. Both group summaries before graphs; the detailed report puts run configuration and source files last. Reports are regenerated when new task data or a newer report layout is available; `results <experiment>` lists run directories and reports.

Run measurements use Parquet. Locust CSV exports remain under each run’s `raw/` directory and are converted to Parquet before analysis. Graph SVGs live under `graphs/` in the run or execution directory. `execution.log` remains for diagnostics; `timings.parquet` holds phase measurements for reports.

`squeeze --run` generates reports for completed measurements and archives the execution when it finishes. `report` lists available execution IDs. `report <execution_id>` generates or reuses that execution's reports; add `--archive` to archive its reports and source data. The separate `interactive.html` embeds uPlot for one selectable run per candidate in side-by-side columns. The first group contains request, task, and combined PostgreSQL CPU and memory charts; a combined content-worker read and write chart follows. The last group repeats request activity, then combines API-worker CPU with memory, API-worker read with write, and temporary-file count with size. Charts share an elapsed-time cursor within each scenario and shade the interval from the first request start to the last request completion, with fixed lines at those boundaries. The report works offline.

`summary_statistics.py` declares the random variables and computes per-run summaries from normalized request, task, collector, and trial-timing rows. Event measurements use population SD and interpolated percentiles. Collector measurements use interval overlap with the request window as weights; their quantiles are time-occupancy quantiles. Candidate summaries take each available run statistic as one observation and use sample SD. The browser switches between selected-run and candidate summaries without recomputing the statistics. The interactive report cache tracks the summary module and execution timings.
