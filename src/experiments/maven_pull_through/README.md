# Maven pull-through experiment

`experiment.py` defines fixture instances, candidates, scenarios, and run order.
Candidate IDs are positive integers.
Each scenario uses its own `request_count`, which may be less than the fixture capacity.
The burst scenario runs each candidate three times; the moderate scenario runs each twice.
`"r"` removes Pulp volumes before the next run. The generated fixture data remains on disk.

## Candidate sources

Every candidate declares a plugin and Git source.
A relative source uses the repository root, so `../pulp_maven` selects the sibling checkout.
A Git URL selects a remote ref; replace the example `your-fork` URL with the branch under test.
Infrastructure resolves each source, installs it, and starts Pulp before invoking candidate setup.

## Files

- `experiment.py` declares the `EXPERIMENT` dataclass instance using shared types from `pulp_juicer`.
- Each fixture is an experiment-specific class derived from `ServiceFixture` or `VolumeFixture`.
- `compose.yaml` defines Pulp; the runner adds fixture service fragments from required fixture metadata.
- `Containerfile` builds Pulp with the selected pulpcore and plugin source contexts.
- `settings/settings.py` configures Pulp for this experiment's Compose service.
- `candidates.py` defines candidate classes that implement `setup` and configure the Maven resources using `pulp_juicer.PulpClient`.
- `service_maven_fixture/compose.yaml` defines the upstream HTTP service.
- `service_maven_fixture/fixture.py` implements `MavenServiceFixture.build` and `generate_metadata`.
  Run it as a module to generate a small fixture and print its metadata: `python -m experiments.maven_pull_through.service_maven_fixture.fixture --count 10 --size 1024`.
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

`build_test_plan` expands this experiment into ordered `RunMetadata` records. Each record identifies a scenario, candidate, and run ID; carries fixture metadata and load settings; and marks whether Pulp must restart before the run. Planning does not build fixtures or start services.
`run` creates one JSON plan with ordered scenario and unit runs on first use, then selects a scope from it. Locust's `test_start` builds fixtures, starts Pulp, and invokes candidate setup, while `test_stop` destroys the services. The driver returns `RunResultMetadata` with request counts, failures, latency, and artifact paths.
Runs and Locust artifacts go under `_juice/results/{scenario}/{candidate}/{run}`; generated fixtures go under `_juice/fixture/`.

```bash
uv run --locked pulp-juicer ls
uv run --locked pulp-juicer show maven-pull-through
uv run --locked pulp-juicer run maven-pull-through
uv run --locked pulp-juicer run maven-pull-through --scenario burst --run-id run001
uv run --locked --group report pulp-juicer report
```

Omit `--run-id` to run the whole scenario, or use `--all` to run the whole experiment. The example staging candidate still has a placeholder Git URL; set its source in `experiment.py` before running with `--refresh-plan`.
Add `-f` to rerun selected units whose result directories already exist.

