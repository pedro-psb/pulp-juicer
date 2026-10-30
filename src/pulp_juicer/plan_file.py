"""Store one experiment plan with ordered scenario and unit runs."""

import json
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from .catalog import experiments
from .plan import RunMetadata, TestPlanMetadata
from .types import LoadConfig, PluginSource, PulpConnection, ServiceFixtureMetadata, VolumeFixtureMetadata

VERSION = 2


def _unit(run: RunMetadata) -> dict:
    fixtures = {}
    for name, metadata in run.fixtures.items():
        kind = "service" if isinstance(metadata, ServiceFixtureMetadata) else "volume"
        fixtures[name] = {"kind": kind, **asdict(metadata)}
    return {
        "run_id": run.run_id,
        "candidate": asdict(run.candidate),
        "fixtures": fixtures,
        "fixture_builds": [{"class": type(item).__name__, **vars(item)} for item in run.fixture_builds],
        "load": asdict(run.load),
        "driver": run.driver,
        "restart_before": run.restart_before,
        "result_dir": str(run.result_dir),
    }


def write_plan_file(plan: TestPlanMetadata) -> Path:
    """Write a single plan at <workdir>/plan-<experiment>.json."""
    path = plan.workdir / f"plan-{plan.experiment.id}.json"
    scenarios = [
        {"scenario": scenario.id, "runs": [_unit(run) for run in plan.runs if run.scenario_id == scenario.id]}
        for scenario in plan.experiment.scenarios
    ]
    data = {
        "version": VERSION,
        "kind": "plan",
        "experiment": plan.experiment.id,
        "runtime": {
            "repository_root": str(plan.repository_root),
            "workdir": str(plan.workdir),
            "compose_file": str(plan.compose_file),
            "results_dir": str(plan.results_dir),
            "pulp_connection": asdict(plan.pulp_connection),
            "pulpcore_source": plan.pulpcore_source,
            "compose_command": list(plan.compose_command),
            "ready_timeout_seconds": plan.ready_timeout_seconds,
        },
        "scenarios": scenarios,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".plan-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, default=str)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _hydrate_run(unit: dict, scenario_id: str, experiment, templates, candidates, input_builder) -> RunMetadata:
    candidate_data = unit["candidate"]
    candidate_id = candidate_data["id"]
    if candidate_id not in candidates:
        raise ValueError(f"Unknown candidate ID: {candidate_id}")
    candidate = replace(
        candidates[candidate_id],
        **{**candidate_data, "source": PluginSource(**candidate_data["source"])},
    )
    fixtures = {}
    for name, data in unit["fixtures"].items():
        values = {key: value for key, value in data.items() if key != "kind"}
        values["output_dir"] = Path(values["output_dir"])
        if data["kind"] == "service":
            values["compose_file"] = Path(values["compose_file"])
            fixtures[name] = ServiceFixtureMetadata(**values)
        elif data["kind"] == "volume":
            fixtures[name] = VolumeFixtureMetadata(**values)
        else:
            raise ValueError(f"Unknown fixture kind: {data['kind']}")
    builds = []
    for data in unit["fixture_builds"]:
        values = {key: value for key, value in data.items() if key != "class"}
        name = values.pop("name")
        values["output_dir"] = Path(values["output_dir"])
        template = templates[name]
        if type(template).__name__ != data["class"]:
            raise ValueError(f"Fixture class changed for {name}")
        builds.append(type(template)(name=name, **values))
    return RunMetadata(
        experiment_id=experiment.id, scenario_id=scenario_id, candidate=candidate,
        run_id=unit["run_id"], fixtures=fixtures, fixture_builds=tuple(builds),
        load=LoadConfig(**unit["load"]), driver=unit["driver"], input_builder=input_builder,
        restart_before=unit["restart_before"], result_dir=Path(unit["result_dir"]),
    )


def load_plan_file(path: Path, *, scenario_id: str | None = None, run_id: str | None = None) -> TestPlanMetadata:
    """Load the global, scenario, or unit scope from one JSON plan."""
    if run_id is not None and scenario_id is None:
        raise ValueError("A run ID requires a scenario")
    data = json.loads(Path(path).read_text())
    if data.get("version") != VERSION or data.get("kind") != "plan":
        raise ValueError(f"Unsupported plan file: {path}")
    experiment_id = data["experiment"]
    registry = experiments()
    if experiment_id not in registry:
        raise ValueError(f"Unknown experiment: {experiment_id}")
    experiment, _ = registry[experiment_id]
    scenarios = {item.id: item for item in experiment.scenarios}
    candidates = {item.id: item for item in experiment.candidates}
    templates = {item.name: item for item in experiment.fixtures}
    selected = []
    seen_scenarios = set()
    for scenario_data in data["scenarios"]:
        current_id = scenario_data["scenario"]
        if current_id not in scenarios or current_id in seen_scenarios:
            raise ValueError(f"Unknown or duplicate scenario: {current_id}")
        seen_scenarios.add(current_id)
        seen_runs = set()
        for unit in scenario_data["runs"]:
            current_run_id = unit["run_id"]
            if current_run_id in seen_runs:
                raise ValueError(f"Duplicate run ID in {current_id}: {current_run_id}")
            seen_runs.add(current_run_id)
            if scenario_id is None or scenario_id == current_id:
                if run_id is None or run_id == current_run_id:
                    selected.append(_hydrate_run(
                        unit, current_id, experiment, templates, candidates, scenarios[current_id].input,
                    ))
    if scenario_id is not None and scenario_id not in seen_scenarios:
        raise ValueError(f"Unknown scenario: {scenario_id}")
    if not selected:
        raise ValueError(f"No runs selected from {path}")
    runtime = data["runtime"]
    return TestPlanMetadata(
        experiment=experiment, repository_root=Path(runtime["repository_root"]),
        workdir=Path(runtime["workdir"]), compose_file=Path(runtime["compose_file"]),
        results_dir=Path(runtime["results_dir"]),
        pulp_connection=PulpConnection(**runtime["pulp_connection"]),
        pulpcore_source=runtime["pulpcore_source"],
        compose_command=tuple(runtime["compose_command"]),
        ready_timeout_seconds=runtime["ready_timeout_seconds"], runs=tuple(selected),
    )
