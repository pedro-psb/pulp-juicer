# Maven pull-through experiment

`experiment.py` defines the fixture, candidates, scenarios, run order, and primary metric.
Candidate IDs are positive integers, and exactly one candidate is the baseline.
Each scenario uses its own `request_count`, which may be less than the fixture capacity.
The burst scenario runs each candidate three times; the moderate scenario runs each twice.
`"r"` tears down and recreates Pulp before the next run.
The fixture service remains available across Pulp restarts.

## Candidate sources

Every candidate declares a plugin and Git source.
A relative source uses the repository root, so `../pulp_maven` selects the sibling checkout.
A Git URL selects a remote ref; replace the example `your-fork` URL with the branch under test.
Infrastructure resolves each source, installs it, and starts Pulp before invoking candidate setup.

## Files

- `experiment.py` declares the `EXPERIMENT` dataclass instance using shared types from `pulp_juicer`.
- `compose.yaml` is the main Compose file; it includes the fixture service and defines Pulp.
- `Containerfile` builds Pulp with the selected pulpcore and plugin source contexts.
- `candidates.py` defines candidate classes that implement `setup` and configure the Maven resources using `pulp_juicer.PulpClient`.
- `service_maven_fixture/compose.yaml` defines the upstream HTTP service.
- `service_maven_fixture/fixture.py` generates unique Maven artifacts for each run.
- `scenario_input.py` combines fixture and candidate setup results with the load into a serializable request specification.

Infrastructure sets `FIXTURE_DIR` to the generated fixture directory when loading the Compose fragment.
It starts the service and supplies fixture context containing `upstream_url` and `artifacts_per_run`.
The fixture generator writes each run's files into that shared directory and returns artifact paths and checksums.

Each candidate's `setup` method receives a `PulpConnection` from `pulp_juicer`, the fixture context, and a unique run ID.
It uses the library Pulp client to create the remote, repository, and distribution, then returns their references and the distribution base URL.
The scenario input function selects unique paths up to `request_count` and returns requests for the infrastructure `http_get` driver.
The experiment modules do not import Locust.

## Proposed runner interface

The `pulp_juicer` library owns execution, Compose lifecycle, Locust invocation, result storage, and restart handling.
The experiment supplies declarative config and experiment-specific setup and input functions.

```python
from pathlib import Path
from pulp_juicer import ExperimentRunner, PulpConnection

from .experiment import EXPERIMENT

runner = ExperimentRunner(
    experiment=EXPERIMENT,
    compose_file=Path(__file__).resolve().parent / "compose.yaml",
    results_dir=Path("_results"),
    pulp_connection=PulpConnection(
        base_url="http://127.0.0.1:5001",
        username="admin",
        password="password",
    ),
)
```

Choose the operation for the desired scope:

```python
# One isolated measurement for a scenario and candidate.
run_result = runner.run_once(scenario_id="burst", candidate_id=1)

# Every entry in the scenario's runs list, honoring "r" restart markers.
scenario_result = runner.run_scenario(scenario_id="burst")

# Every configured scenario in declaration order.
experiment_result = runner.run_experiment()
```

Each call returns a result object with run metadata and its artifact directory.
`run_once` starts from a fresh Pulp instance; `run_scenario` follows the configured schedule and its restart markers.
`run_experiment` starts each scenario clean while keeping the experiment fixture service available.
The runner supports `with ExperimentRunner(...) as runner` so exiting the context stops services it started.
