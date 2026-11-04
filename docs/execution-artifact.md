# Execution artifact specification (draft)

This is a proposed contract for a future artifact layout. The current layout and code do not yet implement it. The execution artifact is the durable result of a test execution. Its primary product is reusable data; the bundled reports are one optional way to view that data.

## Goals

- Make collection, preparation, and reporting separate operations with visible outputs and failure states.
- Let someone analyze or visualize an execution using only its artifact, without the running services, experiment source tree, or pulp-juicer report code.
- Keep observations at their original grain. Publish derived series and statistics alongside them, with their definitions, so users can reproduce or replace the default analysis.
- Allow report generation to be repeated or replaced without changing the prepared data.
- Keep failed and incomplete runs discoverable without presenting partial measurements as complete.

## Artifact layout

```text
_juice/executions/<execution-id>/
  artifact.json                       identity, versions, state, inventory
  capture/                           collection output and diagnostics
    execution.log
    runs/<scenario-id>/<candidate-id>/<run-id>/
      run_metadata.json               recorded configuration and provenance
      input.parquet                    selected workload, if applicable
      requests.parquet                 request recorder output
      locust/*.csv                     original Locust exports
      collectors/*.parquet             original collector output
      pulp_status.json
      compose.log
      errors/*.txt                     collection failures, when present
    sources/                           optional source snapshots
  data/                              prepared, documented analysis data
    <preparation-id>/                 active revision named by artifact.json
      catalog.json                    table, column, unit, and derivation definitions
      runs.parquet                    one row per planned run
      trial_phases.parquet            one row per run
      observations/
        workload/<run-partitions>/*.parquet
        requests/<run-partitions>/*.parquet
        tasks/<run-partitions>/*.parquet
        locust_history/<run-partitions>/*.parquet
        postgres/<run-partitions>/*.parquet
        api_workers/<run-partitions>/*.parquet
        content_io/<run-partitions>/*.parquet
        temporary_files/<run-partitions>/*.parquet
      derived/
        time_series.parquet           reusable elapsed-time series
        run_statistics.parquet        statistics within each run
        candidate_statistics.parquet  distributions across runs
        task_type_statistics.parquet   task counts and durations by type
  reports/                           replaceable presentation output
    manifest.json                     report files and preparation ID used
    summary.html
    interactive.html
    trials.html
    report.html
    graphs/                           optional exported images
```

`<run-partitions>` means `scenario=<id>/candidate=<id>/run=<id>`. The logical columns also include these identifiers; a reader does not have to infer identity from the path. Candidate IDs are stable keys, while names remain display metadata. The exact partition encoding must be specified before implementation so IDs containing path-sensitive characters remain safe.

The root `artifact.json` and its named preparation revision under `data/` are required for a **ready** artifact. `capture/` remains available on disk for audit and re-preparation, but a portable data-only bundle may omit it. `reports/` may be absent. No report is required to read the data.

## Stage contracts

| Stage | Reads | Writes | Completion rule |
|:--|:--|:--|:--|
| Collect | Plan, fixtures, services | `capture/`, collection state in `artifact.json` | Every attempted run has a recorded outcome; captured files are finalized or marked incomplete. |
| Prepare | `capture/` | `data/`, active revision in `artifact.json` | All published tables pass schema and referential checks; `artifact.json` names the exact preparation version and inputs. |
| Report | `data/`, presentation preferences | `reports/` only | Each requested report is written; data files and preparation metadata are untouched. |

The normal `squeeze --run` flow should invoke the three stages in order and produce default reports. They must also be independently rerunnable. A report command should require ready prepared data and tell the user to run preparation if it is absent; it should never silently normalize files or recompute canonical statistics while rendering. Re-preparation publishes a new revision under `data/` from `capture/`; report regeneration may replace `reports/` from the active data revision.

## Prepared data contract

All tables are Parquet except the two small JSON catalogs. Each logical table has a documented grain, primary key or uniqueness rule, nullable fields, units, and producer version. Tables retain real UTC timestamps as timestamp types. Elapsed seconds are measured from the run's recorded load start and are named explicitly (`elapsed_s`). CPU percent means percent of one core and may exceed 100. Memory uses PSS in MiB, not a host-memory percentage.

| Table | Grain | Main content |
|:--|:--|:--|
| `runs` | One planned run | Scenario, candidate ID and name, run ID, order, outcome, load profile, expected/completed requests, failure reason, load start, input start/end, provenance references. Include failed runs. |
| `observations/workload` | One planned workload item | Request inputs and their order. It is separate from observed requests until a durable request ID exists in both streams. |
| `observations/requests` | One observed request | Start and completion UTC, response duration, status, bytes, success, and error. Preserve failed requests. |
| `observations/tasks` | One task snapshot | Task ID, type, status, creation/unblock/start/finish UTC, and available execution timing. |
| `observations/locust_history` | One Locust history sample | Timestamp, users, cumulative requests/failures, and original rate fields. |
| Resource observation tables | One collector sample or worker sample | Timestamp, interval, process identity when applicable, and original measured values with explicit units. Missing samples remain missing, rather than becoming zero. |
| `trial_phases` | One run | Total duration and each named, nonoverlapping phase in seconds; unavailable phases are null. |
| `derived/time_series` | One run, series key, elapsed time | Rates, counts, cumulative counts, and aggregated resource series used by the default timeline. The catalog defines each series and its binning or aggregation. |
| `derived/run_statistics` | One run, variable, statistic | Within-run `n`, mean, SD, percentiles, maximum, total, or percentage where defined. |
| `derived/candidate_statistics` | One scenario, candidate, variable, run statistic, aggregate statistic | Equal-run-weight summaries with `n_runs`, mean, SD, median, minimum, and maximum as applicable. |
| `derived/task_type_statistics` | One run or candidate, task type, statistic | Count and execution duration statistics with the same within-run/across-run distinction. |

The base observation tables are the authoritative measurements. Derived tables are part of the prepared artifact for convenient analysis, but must be reproducible from the base tables and cataloged algorithms. The catalog should record details such as request-window selection, one-second bin boundaries, collector interval weighting, percentile method, and how missing values affect `n`. A report can pivot or format these tables, but must not introduce new measurement definitions.

## Manifest and readiness

`artifact.json` should contain at least:

- Artifact format version, execution ID, experiment ID, creation time, and tool version.
- The effective execution plan and scenario/candidate/run identities, including an override such as `-s N`. Display choices such as `selected_stats` are recorded as presentation preferences, not as part of the measurement schema.
- Collection state and a per-run outcome. A failed run does not invalidate complete runs, but its missing tables must be explicit.
- Preparation state, the active preparation ID, and its table inventory, with schema version, row count, source inputs, preparation version, and content checksum. The artifact is ready for analysis when an active validated revision exists. Report inventories belong under `reports/`.
- Source revisions and relevant configuration snapshots. Credentials and other secrets must not be stored.

Preparation writes a new revision under `data/`, validates it, then updates `artifact.json` last to name that revision as active. Readers can continue to use the previous active revision while a new one is being prepared. Old revisions may be removed after publication and any reports referring to them have been handled. `reports/manifest.json` records which preparation revision each report rendered.

## Module boundaries proposed for implementation

| Area | Responsibility | Existing code to examine |
|:--|:--|:--|
| Collection and execution | Run services and collectors; write `capture/`; record outcomes. | `execution.py`, `collector/`, `locust_driver.py` |
| Artifact contract | Paths, manifest, schema versions, table catalog, and validation. | `result_data.py`, `plan.py`, `types.py` |
| Preparation | Import captured formats, normalize observations, calculate trial phases, time series, and statistics; publish `data/`. | `result_data.py`, `execution_timing.py`, `summary_statistics.py`, resource metric modules, data portions of `trial_report.py` |
| Reports | Read only prepared tables and render HTML/SVG. | `report.py`, `trial_report.py`, `interactive_report.py`, `summary_report.py` |

The package names can be chosen during implementation. The important dependency direction is **collection → preparation → reports**. Report modules must not import collection code to obtain data or mutate capture/data files. A separate artifact reader should provide one documented API for third-party analysis and for the bundled reports.

## Migration and compatibility

Existing executions keep their current layout. A migration/preparation command should be able to read their run directories, collector Parquet, Locust CSV/Parquet, `run_metadata.json`, and `execution.log`, then produce the new `data/` layout. It must not destroy or silently rewrite the original capture. New report generation should target the new prepared contract; old reports can remain alongside legacy artifacts until migration is requested.

## Decisions to settle before implementation

1. Should the default ZIP contain `capture/` and source snapshots, or should the default be a smaller data-plus-reports bundle with an explicit full archive option? The on-disk artifact should retain capture either way.
2. Should prepared data be published after every completed run, or only once collection of the whole execution ends? Per-run publication improves recovery but needs careful manifest revisions.
3. Is a table-first partitioned `data/observations/` layout preferable to one prepared directory per run? The draft favors table-first layout for cross-run analysis.
4. Do we want a stable public Python reader, a documented SQL/DuckDB query surface, or both as part of the first implementation?
