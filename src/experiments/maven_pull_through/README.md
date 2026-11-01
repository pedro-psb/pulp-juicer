# Maven pull-through and upload experiment

`experiment.py` defines fixture instances, candidates, scenarios, and run order.
Candidate IDs are positive integers.
Each scenario uses its own `request_count`, which may be less than the fixture capacity.
Each candidate has a short description shown by `pulp-juicer show maven-pull-through`.
`UnitaryImpulseLoad(request_count=N)` starts N users, each making one request. `StepFunctionLoad` changes the target user count at each `LoadStep(at_seconds, users)` and derives a spawn rate for each change. Burst and temporary scenarios currently have one step, preserving their user targets and request counts. Duration, waits, and timeouts use profile defaults.
The burst scenario runs normal and staging twice. Pulp volumes are reset before every run; generated fixture data remains on disk.
The `upload-impulse` scenario reuses those JAR files for 100 concurrent multipart POSTs to Maven's synchronous upload endpoint. Each request includes a unique `relative_path`. Staging uploads include the repository so Pulp stages each content unit; normal uploads collect the returned content HREFs and end with one repository modify request that adds them all. HTTP Basic credentials are supplied only at execution time.

## Candidate sources

The experiment requires `pulpcore` and `pulp_maven`; every candidate supplies a source for each.
A relative source uses the repository root, so `../pulpcore` and `../pulp_maven` select sibling checkouts.
A Git URL selects a remote ref; the normal candidate uses `pulp/pulp_maven` at tag `0.37.0`.
The staging candidate supplies its autocommit delay and scan interval as `pulp_settings`; the normal candidate uses Pulp's defaults.
Infrastructure resolves each source, installs it, and starts Pulp before invoking candidate setup.

## Files

- `experiment.py` declares the `EXPERIMENT` dataclass instance using shared types from `pulp_juicer`.
- Each fixture is an experiment-specific class derived from `ServiceFixture` or `VolumeFixture`.
- `compose.yaml` defines Pulp; the runner adds fixture service fragments from required fixture metadata.
- `Containerfile` builds Pulp with the selected pulpcore and plugin source contexts.
- `pulp_juicer/collector` supplies a noarch RPM with the s6 collector service. `PulpComponents` discovers API, content, task worker, and PostgreSQL processes through s6, and exposes Pulp's worker temp directories and database connection settings. Periodic collectors write separate Parquet files: timestamps, PostgreSQL process-tree CPU and memory, per-content-worker storage I/O rates (`content_io.parquet`), per-API-worker CPU, PSS memory, and storage I/O rates (`api_worker.parquet`), and Pulp working-directory files and bytes (`pulp_temp.parquet`). The working directory is shared, so its file counts are not attributable to content workers alone. The collector discovers services as root; its PostgreSQL snapshot helper reads proportional set size (PSS) as the `postgres` user, and its worker I/O helper reads process counters as the `pulp` user. Before teardown, the runner requests an on-demand PostgreSQL task snapshot (`tasking.parquet`) through a Unix socket, finalizes periodic Parquet files, and copies them into the run's `collectors/` directory. Compose logs are retained for debugging.
- `settings/settings.py` applies shared settings and the selected candidate's `pulp_settings`.
- `candidates.py` defines one setup function per candidate, used by their `PreTestFunction` records. They configure Maven resources using `pulp_juicer.PulpClient`.
- `data_service/compose.yaml` defines the upstream HTTP service.
- `data_service/fixture.py` implements `MavenServiceFixture.build` and `generate_metadata`.
  Run it as a module to generate a small fixture and print its metadata: `python -m experiments.maven_pull_through.data_service.fixture --count 10 --size 1024`.
- `scenario_input.py` combines fixture and candidate setup results with the load into a serializable request specification.

The Containerfile builds and installs the collector RPM with `rpm-rs` and installs PyArrow and psutil into the image with `uv`. To build the RPM outside Docker for inspection, run `uv run --locked python src/pulp_juicer/collector/build_rpm.py _juice/rpm` from the repository root.

After pre-test setup, the runner asks the collector to wait up to 2 minutes for two consecutive one-second PostgreSQL CPU samples at zero before starting Locust. After load generation stops, it waits up to 10 minutes for Pulp tasks to reach a final state, excluding scheduled tasks. Both waits report an initial sample and updates every 5 seconds to the CLI and execution log. It then snapshots all task rows and finalizes the periodic collectors. If the task wait times out or fails, it saves `task_wait_error.txt` in the run directory and continues capturing results.

Each scenario declares its fixture dependencies with `RequireFixture(name, args)`.
The runner resolves each `RequireFixture` by name and passes its `args` as keyword arguments to the concrete fixture constructor, along with `name` and `output_dir`.
The constructor defines which fixture-specific options it accepts.
The runner then calls `build()` and `generate_metadata()` on that configured instance.
`ServiceFixtureMetadata` describes the Compose fragment, service name, output directory, and service URL.
`VolumeFixtureMetadata` describes the generated output directory and mount path.
The runner combines the main Compose file with service fragments required by the scenario and mounts fixture output directories.
The Maven fixture writes its artifact manifest into its output directory; scenario input uses that manifest to select requests.

Each candidate declares a list of `PreTestFunction(description, setup_fn)` records. The runner calls each function after starting Pulp, passing a `PulpConnection`, required fixture metadata by name, and a unique run ID. The returned mappings are combined for the scenario input function.
The Maven setup function uses the library Pulp client to create the remote, repository, and distribution, then returns their references and the distribution base URL.
The scenario input functions read the required fixture's manifest and select unique paths up to `request_count`. Pull-through uses the `http_get` driver; upload uses `http_upload` with the same artifact files.
The experiment modules do not import Locust.

## Test plan and CLI

`build_test_plan` expands this experiment's `run_order` into ordered `RunMetadata` records. Each record identifies a scenario, candidate, and run ID and carries fixture metadata and load settings. Planning does not build fixtures or start services.
`squeeze` selects an execution: the whole experiment, one scenario, or one run. It builds the selected plan in memory; `--save-plan` also writes JSON. The Python wrapper builds fixtures, starts Pulp, and invokes candidate setup before Locust begins recording; it destroys the services after Locust finishes and closes its measurements. The driver returns `RunResultMetadata` with request counts, failures, latency, and artifact paths.
Runs and Locust artifacts go under `_juice/exeuctions/{execution_id}/{scenario}/{candidate}/{run}`; generated fixtures go under `_juice/fixture/`.

```bash
uv run --locked pulp-juicer ls
uv run --locked pulp-juicer show maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through --scenario burst --run-id run001
uv run --locked --group report pulp-juicer report
uv run --locked --group report pulp-juicer report maven-pull-through__burst__001
uv run --locked --group report pulp-juicer report maven-pull-through__burst__001 --zip
```

Omit `--run-id` to run the whole scenario, or use `--all` to run the whole experiment. A bare experiment name lists the available runs.
Each squeeze gets a new numbered execution ID. Add `-f` to replace the latest execution for the selected scope.
`report` lists available execution IDs; pass one ID to generate its reports.


Run measurements use Parquet (`requests.parquet`, `data_load.parquet`, Locust exports, and `collectors/*.parquet`). Locust’s original CSV exports are retained under `raw/`; report SVGs are stored under `graphs/`. Trial comparison SVGs and phase timings (`timings.parquet`) live in the execution directory.
