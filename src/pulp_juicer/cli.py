"""Discover and execute Pulp load-test plans."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import click
from rich.console import Console
from rich.text import Text

from .catalog import experiments
from .compose_runtime import ComposeRuntime
from .execution import execute_test_plan
from .execution_ids import latest_execution_id, next_execution_id
from .execution_log import ExecutionLog
from .plan import build_test_plan, execution_scope, resolve_workdir
from .plan_file import write_plan_file
from .report import render_report
from .sources import public_source
from .trial_report import render_trial_report
from .types import PulpConnection


@click.group()
def cli():
    """Manage Pulp load-test plans."""


def _print_styled(*parts):
    line = Text()
    for part in parts:
        if isinstance(part, tuple):
            value, style = part
            line.append(str(value), style=style)
        else:
            line.append(str(part))
    Console(file=sys.stdout, highlight=False).print(line, soft_wrap=True)


def _list_runs(runs):
    current_scenario = None
    for run in runs:
        if run.scenario_id != current_scenario:
            current_scenario = run.scenario_id
            _print_styled("  ", (current_scenario, "bold green"), ":")
        _print_styled(
            "    ", (run.run_id, "yellow"), "  ", (run.candidate.name, "cyan")
        )


def _list_run_columns(runs):
    scenario_width = max(len("SCENARIO"), *(len(run.scenario_id) for run in runs))
    run_width = max(len("RUN_ID"), *(len(run.run_id) for run in runs))
    _print_styled(
        "  ",
        (f"{'SCENARIO':<{scenario_width}}  {'RUN_ID':<{run_width}}  CANDIDATE", "bold"),
    )
    for run in runs:
        _print_styled(
            "  ",
            (f"{run.scenario_id:<{scenario_width}}", "green"),
            "  ",
            (f"{run.run_id:<{run_width}}", "yellow"),
            "  ",
            (run.candidate.name, "cyan"),
        )


def _list_experiments(*, include_scenarios=True):
    for experiment, _ in experiments().values():
        _print_styled((experiment.id, "bold cyan"), ": ", experiment.short_description)
        if include_scenarios:
            for scenario in experiment.scenarios:
                _print_styled("  ", (scenario.id, "green"), ": ", scenario.description)


def _print_post_debug_commands(plan, *, maybe_running=False):
    project = ComposeRuntime(plan).project
    compose = list(plan.compose_command)
    click.echo(
        "\nPost-debug commands (services may be running):"
        if maybe_running
        else "\nPost-debug: services are still running. Try:"
    )
    click.echo(f"  export COMPOSE_PROJECT_NAME={shlex.quote(project)}")
    click.echo(f"  export COMPOSE_FILE={shlex.quote(str(plan.compose_file))}")
    click.echo()
    for args in (
        [*compose, "ps"],
        [*compose, "exec", "pulp", "sh"],
        [*compose, "logs", "--follow", "pulp"],
        [
            "curl",
            "--user",
            plan.pulp_connection.username,
            f"{plan.pulp_connection.base_url.rstrip('/')}/pulp/api/v3/status/",
        ],
        [*compose, "down", "--remove-orphans"],
    ):
        click.echo(f"  {shlex.join(args)}")
    click.echo("curl will prompt for the Pulp API password.")


@cli.command("ls")
def list_experiments():
    """List the available experiments and their scenarios."""
    _list_experiments()


@cli.command("show")
@click.argument("experiment_id")
def show_experiment(experiment_id):
    """Show an experiment's description, scenarios, candidates, and runs."""
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    experiment, compose_file = registry[experiment_id]
    _print_styled((experiment.id, "bold cyan"), ": ", experiment.description)
    _print_styled(
        ("Required sources: ", "bold magenta"), ", ".join(experiment.required_sources)
    )
    _print_styled(("Scenarios:", "bold magenta"))
    for scenario in experiment.scenarios:
        count = len(scenario.run_order)
        _print_styled(
            "  ",
            (scenario.id, "green"),
            ": ",
            scenario.description,
            (f" ({count} runs, {scenario.load.users} users)", "dim"),
        )
    _print_styled(("Candidates:", "bold magenta"))
    for candidate in experiment.candidates:
        _print_styled("  ", (candidate.id, "yellow"), ": ", (candidate.name, "cyan"))
        for name in experiment.required_sources:
            _print_styled(
                "    ", (name, "green"), ": ", public_source(candidate.sources[name])
            )
    try:
        plan = build_test_plan(experiment, compose_file)
    except (ValueError, OSError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    _print_styled(("Runs:", "bold magenta"))
    _list_run_columns(plan.runs)


@cli.command("report")
@click.option(
    "--results",
    "results_dir",
    type=click.Path(path_type=Path),
    default=Path("_juice/results"),
    show_default=True,
)
@click.option(
    "--quarto", "quarto_command", envvar="QUARTO", default="quarto", show_default=True
)
def report_results(results_dir, quarto_command):
    """Render detailed and trial comparison reports per execution."""
    from pyarrow import ArrowException

    from .tasking import analyze_execution_tasking

    new_reports = []
    existing_reports = []
    try:
        if results_dir.is_dir():
            for execution_dir in sorted(results_dir.iterdir()):
                if not execution_dir.is_dir() or not re.fullmatch(
                    r".+__\d{3,}", execution_dir.name
                ):
                    continue
                analyze_execution_tasking(execution_dir)
                for name, renderer in (
                    ("report.html", render_report),
                    ("trials.html", render_trial_report),
                ):
                    output = execution_dir / name
                    previous_time = (
                        output.stat().st_mtime_ns if output.is_file() else None
                    )
                    report = renderer(execution_dir, quarto_command)
                    if report is not None:
                        unchanged = (
                            previous_time is not None
                            and output.stat().st_mtime_ns == previous_time
                        )
                        (existing_reports if unchanged else new_reports).append(report)
    except (
        OSError,
        ValueError,
        KeyError,
        ArrowException,
        subprocess.CalledProcessError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    if not new_reports and not existing_reports:
        click.echo(f"No completed runs found under {results_dir}")
        return
    for title, reports in (
        ("New Reports", new_reports),
        ("Existing", existing_reports),
    ):
        if reports:
            if title == "Existing" and new_reports:
                click.echo()
            click.echo(f"{title}:")
            for report in reports:
                click.echo(report)


@cli.command("results")
@click.argument("experiment_id")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path),
    default=Path("_juice"),
    show_default=True,
)
def list_results(experiment_id, workdir):
    """Print an experiment's result directories and generated report files."""
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    _, compose_file = registry[experiment_id]
    results_dir = resolve_workdir(compose_file, workdir) / "results"
    found = False
    for metadata_file in sorted(results_dir.glob("*/*/*/*/run_metadata.json")):
        try:
            metadata = json.loads(metadata_file.read_text())
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Cannot read {metadata_file}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise click.ClickException(f"Invalid run metadata: {metadata_file}")
        recorded_experiment = metadata.get("experiment")
        if recorded_experiment == experiment_id or (
            recorded_experiment is None
            and metadata.get("project") == f"pulp-juicer-{experiment_id}"
        ):
            _print_styled((metadata_file.parent, "cyan"))
            found = True
    if results_dir.is_dir():
        for execution_dir in sorted(results_dir.iterdir()):
            if (
                not execution_dir.is_dir()
                or not execution_dir.name.startswith(f"{experiment_id}__")
                or not re.fullmatch(r".+__\d{3,}", execution_dir.name)
            ):
                continue
            for name in ("report.html", "report.qmd", "trials.html", "trials.qmd"):
                report_file = execution_dir / name
                if report_file.is_file():
                    _print_styled((report_file, "magenta"))
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
        return subprocess.run(
            [*executable, *args], check=True, capture_output=True, text=True
        ).stdout
    except OSError as exc:
        raise click.ClickException(f"Cannot run Docker: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(exc.stderr.strip() or str(exc)) from exc


def _compose_containers(executable):
    row_format = (
        '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t'
        '{{.Label "com.docker.compose.service"}}\t'
        "{{.Names}}\t{{.Status}}\t{{.Ports}}"
    )
    output = _docker_output(
        executable,
        "container",
        "ls",
        "--all",
        "--filter",
        "label=com.docker.compose.project",
        "--format",
        row_format,
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
        executable,
        "network",
        "ls",
        "--filter",
        "label=com.docker.compose.project",
        "--format",
        row_format,
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
    rows = [
        ("PROJECT", "SERVICE", "CONTAINER", "STATUS", "PORTS"),
        *(service[1:] for service in sorted(services, key=lambda item: item[1:4])),
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    for index, row in enumerate(rows):
        if index == 0:
            _print_styled(
                (
                    "  ".join(
                        f"{value:<{widths[column]}}"
                        for column, value in enumerate(row[:4])
                    )
                    + f"  {row[4]}",
                    "bold",
                )
            )
            continue
        _print_styled(
            (f"{row[0]:<{widths[0]}}", "cyan"),
            "  ",
            (f"{row[1]:<{widths[1]}}", "green"),
            "  ",
            f"{row[2]:<{widths[2]}}",
            "  ",
            (
                f"{row[3]:<{widths[3]}}",
                "green" if row[3].startswith("Up") else "yellow",
            ),
            "  ",
            row[4],
        )


def _clean_targets(value):
    targets = [part.strip() for part in value.split(",")]
    valid = {"services", "results", "fixtures"}
    if targets == ["all"]:
        return valid
    if "all" in targets:
        raise click.BadParameter("'all' cannot be combined with other targets")
    unknown = set(targets) - valid
    if unknown:
        raise click.BadParameter(
            f"Unknown target(s): {', '.join(sorted(unknown))}. "
            "Choose services, results, fixtures, or all"
        )
    return set(targets)


def _remove_output_dir(path):
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            raise click.ClickException(f"Expected a directory: {path}")
        else:
            return False
    except OSError as exc:
        raise click.ClickException(f"Cannot remove {path}: {exc}") from exc
    return True


@cli.command("clean")
@click.argument("targets", required=False)
@click.option(
    "--workdir",
    type=click.Path(path_type=Path),
    default=Path("_juice"),
    show_default=True,
)
@click.option("--docker-command", envvar="DOCKER", default="docker", show_default=True)
def clean(targets, workdir, docker_command):
    """Remove services, results, fixtures, all, or a comma separated selection."""
    if targets is None:
        raise click.UsageError(
            "Missing argument 'TARGETS'.\n"
            "Available targets: services, results, fixtures, all.\n"
            "Combine targets with commas, e.g. results,fixtures."
        )
    selected = _clean_targets(targets)
    root = None
    if selected & {"results", "fixtures"}:
        if workdir.is_absolute():
            root = workdir
        else:
            first_experiment = next(iter(experiments().values()), None)
            if first_experiment is None:
                raise click.ClickException("No experiment available to resolve workdir")
            root = resolve_workdir(first_experiment[1], workdir)
    if "services" in selected:
        executable = _docker_prefix(docker_command)
        containers = _compose_containers(executable)
        networks = _compose_networks(executable)
        if containers:
            _docker_output(
                executable,
                "container",
                "rm",
                "--force",
                *(item[0] for item in containers),
            )
        if networks:
            _docker_output(executable, "network", "rm", *(item[0] for item in networks))
        click.echo(
            f"Removed {len(containers)} container(s) and {len(networks)} network(s)."
        )

    if selected & {"results", "fixtures"}:
        for target, directory in (("results", "results"), ("fixtures", "fixture")):
            if target in selected:
                path = root / directory
                removed = _remove_output_dir(path)
                click.echo(f"{'Removed' if removed else 'No'} {target} at {path}")


@cli.command("squeeze")
@click.argument("experiment_id", required=False)
@click.option(
    "--workdir",
    type=click.Path(path_type=Path),
    default=Path("_juice"),
    show_default=True,
)
@click.option(
    "--all", "run_all", is_flag=True, help="Run every scenario and unit in the plan."
)
@click.option("--scenario", "scenario_id", help="Run one scenario from the plan.")
@click.option("--run-id", help="Run one unit within the selected scenario.")
@click.option(
    "-f", "--force", is_flag=True, help="Replace the latest execution for this scope."
)
@click.option(
    "--save-plan", is_flag=True, help="Save the generated execution plan as JSON."
)
@click.option(
    "--post-debug",
    is_flag=True,
    help="Leave services running after one --run-id measurement.",
)
def squeeze(
    experiment_id, workdir, run_all, scenario_id, run_id, force, save_plan, post_debug
):
    """List available runs, or execute an experiment, scenario, or run."""
    if experiment_id is None:
        _list_experiments(include_scenarios=False)
        raise click.UsageError("Missing argument 'EXPERIMENT_ID'.")
    registry = experiments()
    if experiment_id not in registry:
        raise click.ClickException(f"Unknown experiment: {experiment_id}")
    if run_all and (scenario_id is not None or run_id is not None):
        raise click.ClickException(
            "--all cannot be combined with --scenario or --run-id"
        )
    if run_id is not None and scenario_id is None:
        raise click.UsageError("--run-id requires --scenario")
    if post_debug and run_id is None:
        raise click.UsageError("--post-debug requires --scenario and --run-id")
    experiment, compose_file = registry[experiment_id]
    if not run_all and scenario_id is None:
        _print_styled(
            ("Available runs for ", "bold magenta"),
            (experiment_id, "bold cyan"),
            (":", "bold magenta"),
        )
        _list_runs(build_test_plan(experiment, compose_file).runs)
        click.echo(
            "Use --all, --scenario SCENARIO, or --scenario SCENARIO --run-id RUN_ID to squeeze."
        )
        return
    scope = execution_scope(experiment_id, scenario_id, run_id)
    root = resolve_workdir(compose_file, workdir)
    execution_log = None
    plan = None
    try:
        selected_id = (
            latest_execution_id(root, scope) if force else None
        ) or next_execution_id(root, scope)
        execution_log = ExecutionLog(selected_id)
        execution_log.logger.info("Plan preparation started")
        plan_file = root / f"plan-{selected_id}.json"
        command = tuple(shlex.split(os.environ.get("COMPOSE", "docker compose")))
        if not command:
            raise ValueError("Compose command must not be empty")
        plan = build_test_plan(
            experiment,
            compose_file,
            workdir=workdir,
            execution_id=selected_id,
            scenario_id=scenario_id,
            pulp_connection=PulpConnection(
                password=os.environ.get("PULP_PASSWORD", "password")
            ),
            compose_command=command,
        )
        if run_id is not None:
            selected_runs = tuple(run for run in plan.runs if run.run_id == run_id)
            if not selected_runs:
                raise ValueError(f"Unknown run ID in {scenario_id}: {run_id}")
            plan = replace(plan, runs=selected_runs)
        execution_log.logger.info("Plan preparation finished: %d runs", len(plan.runs))
        if force and plan.results_dir.exists():
            shutil.rmtree(plan.results_dir)
        plan.results_dir.mkdir(parents=True, exist_ok=True)
        log_path = plan.results_dir / "execution.log"
        execution_log.save_to(log_path)
        if save_plan:
            write_plan_file(plan)
        elif force:
            plan_file.unlink(missing_ok=True)
        click.echo(f"Execution: {selected_id}")
        click.echo(f"Log: {log_path}")
        if save_plan:
            click.echo(f"Plan: {plan_file}")
        click.echo(f"Selected {len(plan.runs)} measurement(s)")

        def progress(message):
            execution_log.logger.info(message)
            click.echo(message)

        results = execute_test_plan(
            plan,
            on_progress=progress,
            post_debug=post_debug,
        )
        incomplete = any(
            result.timed_out or result.completed_requests < result.expected_requests
            for result in results
        )
        if incomplete:
            execution_log.logger.warning("Execution finished with incomplete runs")
        else:
            execution_log.logger.info("Execution finished: %d runs", len(plan.runs))
    except (
        ValueError,
        OSError,
        RuntimeError,
        TimeoutError,
        KeyError,
        subprocess.CalledProcessError,
    ) as exc:
        if execution_log is not None:
            execution_log.logger.exception("Execution failed")
        if plan is not None:
            for run in plan.runs:
                compose_log = run.result_dir / "compose.log"
                if compose_log.is_file():
                    click.echo(f"Compose log: {compose_log}", err=True)
            if post_debug:
                _print_post_debug_commands(plan, maybe_running=True)
        raise click.ClickException(str(exc)) from exc
    finally:
        if execution_log is not None:
            execution_log.close()
    for result in results:
        click.echo(
            f"{result.scenario_id}/{result.run_id} {result.candidate_name}: "
            f"{result.completed_requests}/{result.expected_requests} requests, "
            f"{result.failures} failures, p95={result.p95_response_ms:.1f} ms"
        )
        incomplete_run = not (result.result_dir / "run_metadata.json").is_file()
        if incomplete_run:
            click.echo("Incomplete measurements removed after unsuccessful run")
        compose_log = result.result_dir / "compose.log"
        if (incomplete_run or result.failures) and compose_log.is_file():
            click.echo(f"Compose log: {compose_log}")
    if plan.results_dir.exists():
        click.echo(f"\nResults: {plan.results_dir}")
    if post_debug:
        _print_post_debug_commands(plan)
    if incomplete:
        raise click.ClickException("One or more runs were incomplete")


if __name__ == "__main__":
    cli()
