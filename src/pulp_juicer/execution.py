"""Execute a declarative test plan using small fixture, Compose, and Locust steps."""

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .client import PulpClient
from .compose_runtime import ComposeRuntime
from .locust_driver import clean_incomplete_results
from .locust_driver import run as run_locust
from .plan import RunMetadata, RunResultMetadata, TestPlanMetadata
from .sources import resolve_source, source_details, source_environment_name
from .types import (
    ServiceFixtureMetadata,
    StepFunctionLoad,
    UnitaryImpulseLoad,
    VolumeFixtureMetadata,
)


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
    pulp_settings = getattr(run.candidate, "pulp_settings", {})
    if pulp_settings:
        override = (
            plan.results_dir
            / "_compose"
            / f"{run.scenario_id}-candidate-{run.candidate.id}-settings.yaml"
        )
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "pulp": {
                            "environment": {
                                "JUICER_PULP_SETTINGS_JSON": json.dumps(pulp_settings)
                            }
                        }
                    }
                },
                indent=2,
            )
            + "\n"
        )
        files.append(override)
    return tuple(dict.fromkeys(files)), environment


def _write_run_context(
    run, result, candidate_data, source_paths, project, execution_id
):
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
        "execution_id": execution_id,
        "run_id": run.run_id,
        "scenario": run.scenario_id,
        "candidate": run.candidate.name,
        "candidate_id": run.candidate.id,
        "pre_test_functions": [
            hook.description for hook in run.candidate.pre_test_functions
        ],
        "pulp_settings": dict(getattr(run.candidate, "pulp_settings", {})),
        "fixture": fixture_info,
        "load": {
            "profile": type(run.load).__name__,
            "users": run.load.users,
            "spawn_rate": run.load.spawn_rate_per_second
            if isinstance(run.load, UnitaryImpulseLoad)
            else None,
            "duration": run.load.max_duration,
            "wait_seconds": run.load.wait_seconds,
            "idle_seconds": run.load.idle_before_seconds,
            "tail_seconds": run.load.tail_after_seconds,
            "request_timeout_seconds": run.load.request_timeout_seconds,
            "request_count": run.load.request_count,
            "steps": [asdict(step) for step in run.load.steps]
            if isinstance(run.load, StepFunctionLoad)
            else [],
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
            "load_start_utc": getattr(result, "load_start_utc", None),
        },
    }
    (output / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def _write_pulp_status(connection, result_dir: Path) -> Path:
    """Capture Pulp's status before its service is stopped."""
    status = PulpClient(connection).get("/pulp/api/v3/status/")
    path = result_dir / "pulp_status.json"
    path.write_text(json.dumps(status, indent=2) + "\n")
    return path


def execute_test_plan(
    plan: TestPlanMetadata,
    *,
    monitor_cpu: bool = True,
    force: bool = False,
    post_debug: bool = False,
    on_skip: Callable[[RunMetadata], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[RunResultMetadata, ...]:
    """Run every record in order and return one result record per measurement."""
    if post_debug and len(plan.runs) != 1:
        raise ValueError("Post-debug requires exactly one run")
    runtime = ComposeRuntime(plan)
    built = set()
    sources = {}
    results = []

    def source_path(source: str, name: str) -> Path:
        key = (source, name)
        if key not in sources:
            if on_progress:
                on_progress(f"Resolving source {name}")
            sources[key] = resolve_source(
                source, name, plan.repository_root, plan.results_dir
            )
        return sources[key]

    try:
        for run in plan.runs:
            existing_results = run.result_dir.exists()
            if (
                existing_results
                and (run.result_dir / "run_metadata.json").is_file()
                and not force
            ):
                if on_skip:
                    on_skip(run)
                continue
            context = {}
            if on_progress:
                on_progress(
                    f"Starting {run.scenario_id}/{run.run_id} {run.candidate.name}"
                )
                on_progress("Pre-test hooks started")
                on_progress("Resetting service volumes")
            if existing_results:
                if on_progress:
                    on_progress(f"Replacing existing results at {run.result_dir}")
                shutil.rmtree(run.result_dir)

            def start_run(run=run, context=context):
                runtime.destroy_before_start(reset=True)
                for fixture in run.fixture_builds:
                    if fixture.output_dir not in built:
                        if on_progress:
                            on_progress(
                                f"Preparing fixture {fixture.name}: {fixture.output_dir}"
                            )
                        fixture.build()
                        built.add(fixture.output_dir)
                        if on_progress:
                            on_progress(f"Prepared fixture {fixture.name}")
                    elif on_progress:
                        on_progress(
                            f"Skipping fixture {fixture.name}: already prepared"
                        )
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
                if on_progress:
                    on_progress("Starting services")
                runtime.ensure(files, environment, source_paths)
                candidate_run_id = (
                    f"{run.scenario_id}-{run.run_id}-{run.candidate.name}"
                )
                candidate_data = {}
                for hook in run.candidate.pre_test_functions:
                    if on_progress:
                        on_progress(f"Pre-test: {hook.description}")
                    output = hook.setup_fn(
                        plan.pulp_connection, run.fixtures, candidate_run_id
                    )
                    if output is not None:
                        candidate_data.update(output)
                context.update(
                    candidate_data=candidate_data,
                    source_paths=source_paths,
                )
                request_input = run.input_builder(
                    run.fixtures, candidate_data, run.load
                )
                if on_progress:
                    on_progress("Pre-test hooks finished")
                return request_input

            def stop_run(run=run, context=context):
                if on_progress:
                    on_progress("Post-test hooks started")
                try:
                    if "candidate_data" in context:
                        if on_progress:
                            on_progress("Capturing Pulp status")
                        try:
                            status_path = _write_pulp_status(
                                plan.pulp_connection, run.result_dir
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            (run.result_dir / "pulp_status_error.txt").write_text(
                                f"{type(exc).__name__}: {exc}\n"
                            )
                            if on_progress:
                                on_progress(f"Pulp status unavailable: {exc}")
                        else:
                            if on_progress:
                                on_progress(f"Saved Pulp status: {status_path}")
                finally:
                    try:
                        if on_progress:
                            on_progress("Capturing Compose logs")
                        log_path = runtime.dump_logs(run.result_dir / "compose.log")
                        if on_progress:
                            on_progress(f"Saved Compose logs: {log_path}")
                    finally:
                        if post_debug:
                            if on_progress:
                                on_progress("Keeping services running for post-debug")
                        else:
                            runtime.close()
                if on_progress:
                    on_progress("Post-test hooks finished")

            result = run_locust(
                run,
                start_run,
                stop_run,
                compose_command=plan.compose_command,
                project=runtime.project,
                compose_file=plan.compose_file,
                monitor_cpu=monitor_cpu,
                on_progress=on_progress,
            )
            if (
                not result.timed_out
                and result.completed_requests >= result.expected_requests
            ):
                try:
                    _write_run_context(
                        run,
                        result,
                        context["candidate_data"],
                        context["source_paths"],
                        runtime.project,
                        plan.execution_id,
                    )
                except BaseException:
                    clean_incomplete_results(run.result_dir)
                    raise
            results.append(result)
            if on_progress:
                on_progress(
                    f"Finished {run.scenario_id}/{run.run_id} {run.candidate.name}: "
                    f"{result.completed_requests}/{result.expected_requests} requests, "
                    f"{result.failures} failures"
                )
    finally:
        if not post_debug:
            runtime.close()
    return tuple(results)
