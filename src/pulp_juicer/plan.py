"""Expand an experiment declaration into an ordered, side-effect-free test plan."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .types import (
    CandidateConfig,
    ExperimentConfig,
    Fixture,
    FixtureMetadata,
    LoadConfig,
    PulpConnection,
    ScenarioConfig,
    ServiceFixtureMetadata,
)


@dataclass(frozen=True)
class RunMetadata:
    """All inputs needed to execute one (scenario, candidate, run_id) measurement."""

    experiment_id: str
    scenario_id: str
    candidate: CandidateConfig
    run_id: str
    fixtures: Mapping[str, FixtureMetadata]
    fixture_builds: tuple[Fixture, ...]
    load: LoadConfig
    driver: str
    input_builder: Callable[..., dict]
    restart_before: bool
    result_dir: Path


@dataclass(frozen=True)
class TestPlanMetadata:
    experiment: ExperimentConfig
    repository_root: Path
    workdir: Path
    compose_file: Path
    results_dir: Path
    pulp_connection: PulpConnection
    compose_command: tuple[str, ...]
    ready_timeout_seconds: float
    runs: tuple[RunMetadata, ...]


@dataclass(frozen=True)
class RunResultMetadata:
    experiment_id: str
    scenario_id: str
    candidate_id: int
    candidate_name: str
    run_id: str
    result_dir: Path
    expected_requests: int
    completed_requests: int
    failures: int
    mean_response_ms: float
    p95_response_ms: float
    timed_out: bool


def _repository_root(compose_file: Path) -> Path:
    for parent in compose_file.parents:
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "pulp_juicer"
        ).is_dir():
            return parent
    raise ValueError(f"Cannot find repository root above {compose_file}")


def resolve_workdir(
    compose_file: Path, workdir: Path, repository_root: Path | None = None
) -> Path:
    """Resolve a plan workspace from the experiment's repository root."""
    workdir = Path(workdir)
    root = (
        Path(repository_root).resolve()
        if repository_root
        else _repository_root(Path(compose_file).resolve())
    )
    return workdir if workdir.is_absolute() else root / workdir


def _fixtures(scenario: ScenarioConfig, experiment: ExperimentConfig, workdir: Path):
    templates = {fixture.name: fixture for fixture in experiment.fixtures}
    metadata = {}
    builds = []
    for requirement in scenario.requires:
        template = templates[requirement.name]
        fingerprint = hashlib.sha256(
            json.dumps(requirement.args, sort_keys=True).encode()
        ).hexdigest()[:12]
        output_dir = template.output_dir
        if not output_dir.is_absolute():
            output_dir = workdir / output_dir
        fixture = type(template)(
            name=template.name, output_dir=output_dir / fingerprint, **requirement.args
        )
        details = fixture.generate_metadata()
        if details.name != requirement.name:
            raise ValueError(
                f"Fixture metadata name {details.name!r} differs from {requirement.name!r}"
            )
        if isinstance(details, ServiceFixtureMetadata):
            environment = dict(details.environment)
            if "FIXTURE_DIR" in environment:
                environment["FIXTURE_DIR"] = str(details.output_dir)
            details = replace(
                details,
                compose_file=details.compose_file.resolve(),
                environment=environment,
            )
        metadata[requirement.name] = details
        builds.append(fixture)
    return metadata, tuple(builds)


def build_test_plan(
    experiment: ExperimentConfig,
    compose_file: Path,
    *,
    workdir: Path = Path("_juice"),
    results_dir: Path | None = None,
    repository_root: Path | None = None,
    pulp_connection: PulpConnection | None = None,
    compose_command: tuple[str, ...] = ("docker", "compose"),
    ready_timeout_seconds: float = 300,
    scenario_id: str | None = None,
    candidate_id: int | None = None,
    run_id: str | None = None,
) -> TestPlanMetadata:
    compose_file = Path(compose_file).resolve()
    root = (
        Path(repository_root).resolve()
        if repository_root
        else _repository_root(compose_file)
    )
    workdir = resolve_workdir(compose_file, workdir, root)
    results_dir = Path(results_dir) if results_dir is not None else workdir / "results"
    if not results_dir.is_absolute():
        results_dir = root / results_dir
    if run_id is not None:
        if scenario_id is None or candidate_id is None:
            raise ValueError("An explicit run ID needs both a scenario and candidate")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError(
                "Run ID must use letters, numbers, underscores, or hyphens"
            )

    candidates = {candidate.id: candidate for candidate in experiment.candidates}
    scenarios = {scenario.id: scenario for scenario in experiment.scenarios}
    if scenario_id is not None and scenario_id not in scenarios:
        raise ValueError(f"Unknown scenario: {scenario_id}")
    if candidate_id is not None and candidate_id not in candidates:
        raise ValueError(f"Unknown candidate ID: {candidate_id}")

    runs = []
    for scenario in experiment.scenarios:
        if scenario_id is not None and scenario.id != scenario_id:
            continue
        fixtures, builds = _fixtures(scenario, experiment, workdir)
        restart_before = True
        next_number = 1
        for entry in scenario.runs:
            if entry == "r":
                restart_before = True
                continue
            if not isinstance(entry, int) or entry not in candidates:
                raise ValueError(f"Invalid candidate ID in {scenario.id}: {entry!r}")
            if candidate_id is not None and entry != candidate_id:
                continue
            if run_id is not None:
                selected_id = run_id
            else:
                selected_id = f"run{next_number:03d}"
                next_number += 1
            result_dir = (
                results_dir / scenario.id / candidates[entry].name / selected_id
            )
            runs.append(
                RunMetadata(
                    experiment_id=experiment.id,
                    scenario_id=scenario.id,
                    candidate=candidates[entry],
                    run_id=selected_id,
                    fixtures=fixtures,
                    fixture_builds=builds,
                    load=scenario.load,
                    driver=scenario.driver,
                    input_builder=scenario.input,
                    restart_before=restart_before,
                    result_dir=result_dir,
                )
            )
            restart_before = False
            if run_id is not None:
                break
    if not runs:
        raise ValueError("The selected test plan has no runs")
    return TestPlanMetadata(
        experiment=experiment,
        repository_root=root,
        workdir=workdir,
        compose_file=compose_file,
        results_dir=results_dir,
        pulp_connection=pulp_connection or PulpConnection(),
        compose_command=tuple(compose_command),
        ready_timeout_seconds=ready_timeout_seconds,
        runs=tuple(runs),
    )
