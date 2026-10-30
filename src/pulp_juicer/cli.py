"""Discover and execute Pulp load-test plans."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import click

from .catalog import experiments
from .execution import execute_test_plan
from .plan import build_test_plan, resolve_workdir
from .plan_file import load_plan_file, write_plan_file
from .report import render_report
from .types import PulpConnection


@click.group()
def cli():
    """Manage Pulp load-test plans."""


def _list_runs(runs):
    current_scenario = None
    for run in runs:
        if run.scenario_id != current_scenario:
            current_scenario = run.scenario_id
            click.echo(f"  {current_scenario}:")
        click.echo(f"    {run.run_id}  {run.candidate.name}")


def _list_run_columns(runs):
    scenario_width = max(len("SCENARIO"), *(len(run.scenario_id) for run in runs))
    run_width = max(len("RUN_ID"), *(len(run.run_id) for run in runs))
    click.echo(f"  {'SCENARIO':<{scenario_width}}  {'RUN_ID':<{run_width}}  CANDIDATE")
    for run in runs:
        click.echo(f"  {run.scenario_id:<{scenario_width}}  {run.run_id:<{run_width}}  {run.candidate.name}")


@cli.command("ls")
def list_experiments():
    """List the available experiments and their scenarios."""
    for experiment, _ in experiments().values():
        click.echo(f"{experiment.id}: {experiment.short_description}")
        for scenario in experiment.scenarios:
            click.echo(f"  {scenario.id}: {scenario.description}")


@cli.command("show")
@click.argument("experiment_id")
def show_experiment(experiment_id):
    """Show an experiment's description, scenarios, candidates, and runs."""
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    experiment, compose_file = registry[experiment_id]
    click.echo(f"{experiment.id}: {experiment.description}")
    click.echo("Scenarios:")
    for scenario in experiment.scenarios:
        count = sum(isinstance(value, int) for value in scenario.runs)
        click.echo(f"  {scenario.id}: {scenario.description} ({count} runs, {scenario.load.users} users)")
    click.echo("Candidates:")
    for candidate in experiment.candidates:
        click.echo(f"  {candidate.id}: {candidate.name}")
    try:
        plan = build_test_plan(experiment, compose_file)
    except (ValueError, OSError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Runs:")
    _list_run_columns(plan.runs)


@cli.command("report")
@click.option("--results", "results_dir", type=click.Path(path_type=Path),
              default=Path("_juice/results"), show_default=True)
@click.option("--quarto", "quarto_command", envvar="QUARTO", default="quarto", show_default=True)
def report_results(results_dir, quarto_command):
    """Render the completed runs as a Quarto HTML report."""
    try:
        report = render_report(results_dir, quarto_command)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc
    if report is None:
        click.echo(f"No completed runs found under {results_dir}")
    else:
        click.echo(f"Report: {report}")


@cli.command("results")
@click.argument("experiment_id")
@click.option("--workdir", type=click.Path(path_type=Path), default=Path("_juice"), show_default=True)
def list_results(experiment_id, workdir):
    """Print existing result directories for an experiment."""
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    _, compose_file = registry[experiment_id]
    results_dir = resolve_workdir(compose_file, workdir) / "results"
    found = False
    for metadata_file in sorted(results_dir.glob("*/*/*/run_metadata.json")):
        try:
            metadata = json.loads(metadata_file.read_text())
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Cannot read {metadata_file}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise click.ClickException(f"Invalid run metadata: {metadata_file}")
        recorded_experiment = metadata.get("experiment")
        if (recorded_experiment == experiment_id or
                (recorded_experiment is None and metadata.get("project") == f"pulp-juicer-{experiment_id}")):
            click.echo(metadata_file.parent)
            found = True
    if not found:
        click.echo(f"No results found for {experiment_id} under {results_dir}")


def _docker_prefix(docker_command):
    try:
        command = shlex.split(docker_command)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not command:
        raise click.ClickException("Docker command must not be empty")
    return command


def _docker_output(executable, *args):
    try:
        return subprocess.run([*executable, *args], check=True, capture_output=True, text=True).stdout
    except OSError as exc:
        raise click.ClickException(f"Cannot run Docker: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(exc.stderr.strip() or str(exc)) from exc


def _compose_containers(executable):
    row_format = (
        '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t'
        '{{.Label "com.docker.compose.service"}}\t'
        '{{.Names}}\t{{.Status}}\t{{.Ports}}'
    )
    output = _docker_output(
        executable, "container", "ls", "--all",
        "--filter", "label=com.docker.compose.project", "--format", row_format,
    )
    services = []
    for line in output.splitlines():
        fields = line.split("\t", 5)
        if len(fields) != 6 or not re.fullmatch(r"[0-9a-f]{12,64}", fields[0]):
            raise click.ClickException(f"Unexpected Docker output: {line}")
        if fields[1].startswith("pulp-juicer-"):
            services.append(fields)
    return services


def _compose_networks(executable):
    row_format = '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t{{.Name}}'
    output = _docker_output(
        executable, "network", "ls", "--filter", "label=com.docker.compose.project",
        "--format", row_format,
    )
    networks = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{12,64}", fields[0]):
            raise click.ClickException(f"Unexpected Docker output: {line}")
        if fields[1].startswith("pulp-juicer-"):
            networks.append(fields)
    return networks


@cli.command("ps")
@click.option("--docker-command", envvar="DOCKER", default="docker", show_default=True)
def list_services(docker_command):
    """Show existing containers from pulp-juicer Compose projects."""
    services = _compose_containers(_docker_prefix(docker_command))
    if not services:
        click.echo("No pulp-juicer services found.")
        return
    rows = [("PROJECT", "SERVICE", "CONTAINER", "STATUS", "PORTS"),
            *(service[1:] for service in sorted(services, key=lambda item: item[1:4]))]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    for row in rows:
        click.echo("  ".join(f"{value:<{widths[index]}}" for index, value in enumerate(row[:4])) + f"  {row[4]}")


@cli.command("clean")
@click.option("--docker-command", envvar="DOCKER", default="docker", show_default=True)
def clean_services(docker_command):
    """Remove all pulp-juicer containers and Compose networks."""
    executable = _docker_prefix(docker_command)
    containers = _compose_containers(executable)
    networks = _compose_networks(executable)
    if containers:
        _docker_output(executable, "container", "rm", "--force", *(item[0] for item in containers))
    if networks:
        _docker_output(executable, "network", "rm", *(item[0] for item in networks))
    click.echo(f"Removed {len(containers)} container(s) and {len(networks)} network(s).")


@cli.command("run")
@click.argument("experiment_id")
@click.option("--workdir", type=click.Path(path_type=Path), default=Path("_juice"), show_default=True)
@click.option("--all", "run_all", is_flag=True, help="Run every scenario and unit in the plan.")
@click.option("--scenario", "scenario_id", help="Run one scenario from the plan.")
@click.option("--run-id", help="Run one unit within the selected scenario.")
@click.option("-f", "--force", is_flag=True, help="Rerun selected units even if results already exist.")
@click.option("--refresh-plan", is_flag=True, help="Rebuild the stored plan from the experiment.")
def run_plan(experiment_id, workdir, run_all, scenario_id, run_id, force, refresh_plan):
    """List available runs, or execute a selected experiment scope."""
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    if run_all and (scenario_id is not None or run_id is not None):
        raise click.ClickException("--all cannot be combined with --scenario or --run-id")
    experiment, compose_file = registry[experiment_id]
    plan_file = resolve_workdir(compose_file, workdir) / f"plan-{experiment_id}.json"
    try:
        if refresh_plan or not plan_file.is_file():
            previous = load_plan_file(plan_file) if plan_file.is_file() else None
            command = previous.compose_command if previous else tuple(shlex.split(os.environ.get("COMPOSE", "docker compose")))
            if not command:
                raise ValueError("Compose command must not be empty")
            prepared = build_test_plan(
                experiment, compose_file, workdir=workdir,
                pulp_connection=(previous.pulp_connection if previous else
                                 PulpConnection(password=os.environ.get("PULP_PASSWORD", "password"))),
                pulpcore_source=previous.pulpcore_source if previous else os.environ.get("PULPCORE_REPO", "../pulpcore"),
                compose_command=command,
                ready_timeout_seconds=previous.ready_timeout_seconds if previous else 300,
            )
            write_plan_file(prepared)
            click.echo(f"Prepared {len(prepared.runs)} runs: {plan_file}")
        plan = load_plan_file(plan_file, scenario_id=scenario_id, run_id=run_id)
        if not run_all and scenario_id is None:
            click.echo(f"Available runs for {experiment_id}:")
            _list_runs(plan.runs)
            click.echo("Use --all, --scenario SCENARIO, or --scenario SCENARIO --run-id RUN_ID to run.")
            return
        click.echo(f"Selected {len(plan.runs)} measurement(s) from {plan_file}")
        results = execute_test_plan(
            plan,
            force=force,
            on_progress=click.echo,
            on_skip=lambda run: click.echo(
                f"Skipping {run.scenario_id}/{run.run_id} {run.candidate.name}: "
                f"results already exist at {run.result_dir}"
            ),
        )
    except (ValueError, OSError, RuntimeError, TimeoutError, KeyError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc
    for result in results:
        click.echo(
            f"{result.scenario_id}/{result.run_id} {result.candidate_name}: "
            f"{result.completed_requests}/{result.expected_requests} requests, "
            f"{result.failures} failures, p95={result.p95_response_ms:.1f} ms"
        )
        if result.result_dir.exists():
            click.echo(f"Results: {result.result_dir}")
        else:
            click.echo("Results removed after unsuccessful run")
    if any(result.timed_out or result.failures or result.completed_requests < result.expected_requests for result in results):
        raise click.ClickException("One or more runs were incomplete or had request failures")


if __name__ == "__main__":
    cli()
