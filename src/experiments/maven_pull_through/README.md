# Maven pull-through experiment

`experiment.py` defines fixture instances, candidates, scenarios, and run order.
Candidate IDs are positive integers.
Each scenario uses its own `request_count`, which may be less than the fixture capacity.
The burst scenario runs normal and staging twice. Pulp volumes are reset before every run; generated fixture data remains on disk.

## Candidate sources

The experiment requires `pulpcore` and `pulp_maven`; every candidate supplies a source for each.
A relative source uses the repository root, so `../pulpcore` and `../pulp_maven` select sibling checkouts.
A Git URL selects a remote ref; the staging candidate uses `pulp/pulp_maven` at tag `0.37.0`.
Infrastructure resolves each source, installs it, and starts Pulp before invoking candidate setup.

## Files

- `experiment.py` declares the `EXPERIMENT` dataclass instance using shared types from `pulp_juicer`.
- Each fixture is an experiment-specific class derived from `ServiceFixture` or `VolumeFixture`.
- `compose.yaml` defines Pulp; the runner adds fixture service fragments from required fixture metadata.
- `Containerfile` builds Pulp with the selected pulpcore and plugin source contexts.
- `settings/settings.py` configures Pulp for this experiment's Compose service.
- `candidates.py` defines candidate classes that implement `setup` and configure the Maven resources using `pulp_juicer.PulpClient`.
- `data_service/compose.yaml` defines the upstream HTTP service.
- `data_service/fixture.py` implements `MavenServiceFixture.build` and `generate_metadata`.
  Run it as a module to generate a small fixture and print its metadata: `python -m experiments.maven_pull_through.data_service.fixture --count 10 --size 1024`.
- `scenario_input.py` combines fixture and candidate setup results with the load into a serializable request specification.

Each scenario declares its fixture dependencies with `RequireFixture(name, args)`.
The runner resolves each `RequireFixture` by name and passes its `args` as keyword arguments to the concrete fixture constructor, along with `name` and `output_dir`.
The constructor defines which fixture-specific options it accepts.
The runner then calls `build()` and `generate_metadata()` on that configured instance.
`ServiceFixtureMetadata` describes the Compose fragment, service name, output directory, and service URL.
`VolumeFixtureMetadata` describes the generated output directory and mount path.
The runner combines the main Compose file with service fragments required by the scenario and mounts fixture output directories.
The Maven fixture writes its artifact manifest into its output directory; scenario input uses that manifest to select requests.

Each candidate's `setup` method receives a `PulpConnection` from `pulp_juicer`, a mapping of required fixture metadata by name, and a unique run ID.
It uses the library Pulp client to create the remote, repository, and distribution, then returns their references and the distribution base URL.
The scenario input function reads the required fixture's manifest, selects unique paths up to `request_count`, and returns requests for the infrastructure `http_get` driver.
The experiment modules do not import Locust.

## Test plan and CLI

`build_test_plan` expands this experiment's `run_order` into ordered `RunMetadata` records. Each record identifies a scenario, candidate, and run ID and carries fixture metadata and load settings. Planning does not build fixtures or start services.
`squeeze` selects an execution: the whole experiment, one scenario, or one run. It builds the selected plan in memory; `--save-plan` also writes JSON. The Python wrapper builds fixtures, starts Pulp, and invokes candidate setup before Locust begins recording; it destroys the services after Locust finishes and closes its measurements. The driver returns `RunResultMetadata` with request counts, failures, latency, and artifact paths.
Runs and Locust artifacts go under `_juice/results/{execution_id}/{scenario}/{candidate}/{run}`; generated fixtures go under `_juice/fixture/`.

```bash
uv run --locked pulp-juicer ls
uv run --locked pulp-juicer show maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through --scenario burst --run-id run001
uv run --locked --group report pulp-juicer report
```

Omit `--run-id` to run the whole scenario, or use `--all` to run the whole experiment. A bare experiment name lists the available runs.
Each squeeze gets a new numbered execution ID. Add `-f` to replace the latest execution for the selected scope.

