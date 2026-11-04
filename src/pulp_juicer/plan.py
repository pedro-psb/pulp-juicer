"""Expand an experiment declaration into an ordered, side-effect-free test plan."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .execution_ids import EXECUTIONS_DIRNAME
from .sample_sequence import sample_sequence
from .types import (
    CandidateConfig,
    ExperimentConfig,
    Fixture,
    FixtureMetadata,
    LoadProfile,
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
    load: LoadProfile
    driver: str
    input_builder: Callable[..., dict]
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
    execution_id: str


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
    load_start_utc: str | None = None


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


def execution_scope(
    experiment_id: str, scenario_id: str | None = None, candidate: str | None = None
) -> str:
    """Name the experiment, scenario, or ad-hoc candidate execution."""
    if candidate is not None and scenario_id is None:
        raise ValueError("A candidate requires a scenario")
    return "__".join(part for part in (experiment_id, scenario_id, candidate) if part)


def resolve_run_order(
    scenario: ScenarioConfig, candidate_ids: tuple[int, ...], samples: int | None = None
) -> tuple[int, ...]:
    """Resolve a configured sequence, or regenerate one for a sample override."""
    if samples is not None:
        order = sample_sequence(samples, candidate_ids)
    else:
        order = scenario.run_order
    if not order:
        raise ValueError(f"Empty run order in {scenario.id}")
    if any(type(entry) is not int or entry not in candidate_ids for entry in order):
        raise ValueError(f"Invalid candidate ID in {scenario.id}")
    return order


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
    samples: int | None = None,
    execution_id: str | None = None,
) -> TestPlanMetadata:
    compose_file = Path(compose_file).resolve()
    root = (
        Path(repository_root).resolve()
        if repository_root
        else _repository_root(compose_file)
    )
    workdir = resolve_workdir(compose_file, workdir, root)
    if results_dir is None:
        results_dir = workdir / EXECUTIONS_DIRNAME
        if execution_id is not None:
            results_dir /= execution_id
    else:
        results_dir = Path(results_dir)
    if not results_dir.is_absolute():
        results_dir = root / results_dir
    candidates = {candidate.id: candidate for candidate in experiment.candidates}
    scenarios = {scenario.id: scenario for scenario in experiment.scenarios}
    if scenario_id is not None and scenario_id not in scenarios:
        raise ValueError(f"Unknown scenario: {scenario_id}")
    if candidate_id is not None and candidate_id not in candidates:
        raise ValueError(f"Unknown candidate ID: {candidate_id}")
    if candidate_id is not None and scenario_id is None:
        raise ValueError("A candidate requires a scenario")
    if candidate_id is not None and samples is not None:
        raise ValueError("Sample override is not available for an ad-hoc candidate")
    if samples is not None and (type(samples) is not int or samples < 1):
        raise ValueError("Sample count must be a positive integer")

    runs = []
    for scenario in experiment.scenarios:
        if scenario_id is not None and scenario.id != scenario_id:
            continue
        if scenario_id is None and scenario.draft:
            continue
        fixtures, builds = _fixtures(scenario, experiment, workdir)
        order = (
            (candidate_id,)
            if candidate_id is not None
            else resolve_run_order(scenario, tuple(candidates), samples)
        )
        for next_number, entry in enumerate(order, start=1):
            selected_id = f"run{next_number:03d}"
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
                    result_dir=result_dir,
                )
            )
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
        execution_id=execution_id or experiment.id,
    )
