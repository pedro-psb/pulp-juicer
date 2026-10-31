"""Execute a declarative test plan using small fixture, Compose, and Locust steps."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .compose_runtime import ComposeRuntime
from .locust_driver import run as run_locust
from .plan import RunMetadata, RunResultMetadata, TestPlanMetadata
from .sources import resolve_source, source_details, source_environment_name
from .types import ServiceFixtureMetadata, VolumeFixtureMetadata


def _compose_inputs(plan: TestPlanMetadata, run: RunMetadata):
    files = [plan.compose_file]
    environment = {}
    mounts = []
    for fixture in run.fixtures.values():
        if isinstance(fixture, ServiceFixtureMetadata):
            files.append(fixture.compose_file)
            environment.update(
                {key: str(value) for key, value in fixture.environment.items()}
            )
        elif isinstance(fixture, VolumeFixtureMetadata):
            mounts.append(
                {
                    "type": "bind",
                    "source": str(fixture.output_dir),
                    "target": fixture.mount_path,
                    "read_only": fixture.read_only,
                }
            )
    if mounts:
        override = (
            plan.results_dir / "_compose" / f"{run.scenario_id}-volume-fixtures.yaml"
        )
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(
            json.dumps({"services": {"pulp": {"volumes": mounts}}}, indent=2) + "\n"
        )
        files.append(override)
    return tuple(dict.fromkeys(files)), environment


def _write_run_context(run, result, candidate_data, source_paths, project):
    """Keep the run configuration, setup, sources, and result in one metadata file."""
    output = result.result_dir
    fixture_build = run.fixture_builds[0] if run.fixture_builds else None
    fixture = next(iter(run.fixtures.values()), None)
    manifest_path = fixture.output_dir / "manifest.json" if fixture else None
    manifest = None
    if manifest_path and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    count = len(manifest["artifacts"]) if manifest and "artifacts" in manifest else 0
    fixture_info = {
        "count": count,
        "requested_count": getattr(fixture_build, "count", count),
        "requested_fixture_id": fixture.name if fixture else "",
        "requested_payload_bytes": getattr(fixture_build, "size", 0),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest
        else "",
    }
    metadata = {
        "experiment": run.experiment_id,
        "run_id": run.run_id,
        "scenario": run.scenario_id,
        "candidate": run.candidate.name,
        "candidate_id": run.candidate.id,
        "fixture": fixture_info,
        "load": {
            "users": run.load.users,
            "spawn_rate": run.load.spawn_rate_per_second,
            "duration": run.load.max_duration,
            "wait_seconds": run.load.wait_seconds,
            "idle_seconds": run.load.idle_before_seconds,
            "tail_seconds": run.load.tail_after_seconds,
            "request_timeout_seconds": run.load.request_timeout_seconds,
            "request_count": run.load.request_count,
        },
        "sources": {
            name: source_details(source, source_paths[name])
            for name, source in run.candidate.sources.items()
        },
        "project": project,
        "upstream_url": fixture.url
        if isinstance(fixture, ServiceFixtureMetadata)
        else "",
        "setup": candidate_data,
        "result": {
            "expected_requests": result.expected_requests,
            "completed_requests": result.completed_requests,
            "failures": result.failures,
            "mean_response_ms": result.mean_response_ms,
            "p95_response_ms": result.p95_response_ms,
            "timed_out": result.timed_out,
        },
    }
    (output / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def execute_test_plan(
    plan: TestPlanMetadata,
    *,
    monitor_cpu: bool = True,
    force: bool = False,
    on_skip: Callable[[RunMetadata], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[RunResultMetadata, ...]:
    """Run every record in order and return one result record per measurement."""
    runtime = ComposeRuntime(plan)
    built = set()
    sources = {}
    results = []

    def source_path(source: str, name: str) -> Path:
        key = (source, name)
        if key not in sources:
            sources[key] = resolve_source(
                source, name, plan.repository_root, plan.results_dir
            )
        return sources[key]

    try:
        for run in plan.runs:
            existing_results = run.result_dir.exists()
            if existing_results and not force:
                if on_skip:
                    on_skip(run)
                continue
            context = {}
            if on_progress:
                on_progress(
                    f"Starting {run.scenario_id}/{run.run_id} {run.candidate.name}"
                )
            runtime.destroy_before_start(reset=run.restart_before)
            if existing_results:
                if on_progress:
                    on_progress(f"Replacing existing results at {run.result_dir}")
                shutil.rmtree(run.result_dir)

            def start_run(run=run, context=context):
                for fixture in run.fixture_builds:
                    if fixture.output_dir not in built:
                        fixture.build()
                        built.add(fixture.output_dir)
                files, environment = _compose_inputs(plan, run)
                source_paths = {
                    name: source_path(run.candidate.sources[name], name)
                    for name in plan.experiment.required_sources
                }
                environment.update(
                    {
                        **{
                            source_environment_name(name): str(path)
                            for name, path in source_paths.items()
                        },
                        "PULP_PORT": str(
                            urlsplit(plan.pulp_connection.base_url).port or 5001
                        ),
                        "PULP_PASSWORD": plan.pulp_connection.password,
                    }
                )
                runtime.ensure(files, environment, source_paths)
                candidate_run_id = (
                    f"{run.scenario_id}-{run.run_id}-{run.candidate.name}"
                )
                candidate_data = run.candidate.setup(
                    plan.pulp_connection, run.fixtures, candidate_run_id
                )
                context.update(
                    candidate_data=candidate_data,
                    source_paths=source_paths,
                )
                return run.input_builder(run.fixtures, candidate_data, run.load)

            result = run_locust(
                run,
                start_run,
                runtime.close,
                compose_command=plan.compose_command,
                project=runtime.project,
                compose_file=plan.compose_file,
                monitor_cpu=monitor_cpu,
                on_progress=on_progress,
            )
            if (
                not result.timed_out
                and result.failures == 0
                and result.completed_requests >= result.expected_requests
            ):
                try:
                    _write_run_context(
                        run,
                        result,
                        context["candidate_data"],
                        context["source_paths"],
                        runtime.project,
                    )
                except BaseException:
                    shutil.rmtree(run.result_dir)
                    raise
            results.append(result)
    finally:
        runtime.close()
    return tuple(results)
