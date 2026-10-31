"""Run one HTTP GET measurement with Locust's Python API."""

import csv
import hashlib
import itertools
import shlex
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import gevent
from locust import HttpUser, constant, task
from locust.env import Environment
from locust.event import Events
from locust.exception import StopUser
from locust.stats import PERCENTILES_TO_REPORT, StatsCSV, StatsCSVFileWriter
from locust.util.timespan import parse_timespan

from .db_cpu_monitor import DBCpuMonitor
from .plan import RunMetadata, RunResultMetadata


def clean_incomplete_results(output: Path) -> None:
    """Remove partial measurements but retain a captured Compose log."""
    log = output / "compose.log"
    if not log.is_file():
        shutil.rmtree(output)
        return
    for path in output.iterdir():
        if path == log:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def run(
    metadata: RunMetadata,
    on_start: Callable[[], dict],
    on_stop: Callable[[], None],
    *,
    on_progress: Callable[[str], None] | None = None,
    progress_interval_seconds: float = 5,
    compose_command: tuple[str, ...] = ("docker", "compose"),
    project: str = "pulp-juicer",
    compose_file: Path | None = None,
    monitor_cpu: bool = True,
) -> RunResultMetadata:
    """Execute one run and retain artifacts after all requests complete."""
    if metadata.driver != "http_get":
        raise NotImplementedError(f"Unsupported driver: {metadata.driver}")

    output = metadata.result_dir
    output.mkdir(parents=True, exist_ok=False)
    successful = False
    try:
        if on_progress:
            on_progress("Preparing service and requests...")
        try:
            request_input = on_start()
            result = _run_created(
                metadata,
                request_input,
                compose_command=compose_command,
                project=project,
                compose_file=compose_file,
                monitor_cpu=monitor_cpu,
                on_progress=on_progress,
                progress_interval_seconds=progress_interval_seconds,
            )
        except BaseException as exc:
            try:
                on_stop()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve the original failure
                exc.add_note(f"Service teardown also failed: {cleanup_error}")
            raise
        on_stop()
        successful = (
            not result.timed_out
            and result.completed_requests >= result.expected_requests
        )
        return result
    finally:
        if not successful:
            clean_incomplete_results(output)


def _run_created(
    metadata: RunMetadata,
    request_input: dict,
    *,
    compose_command: tuple[str, ...],
    project: str,
    compose_file: Path | None,
    monitor_cpu: bool,
    on_progress: Callable[[str], None] | None,
    progress_interval_seconds: float,
) -> RunResultMetadata:
    """Run Locust inside an output directory owned by this measurement."""
    output = metadata.result_dir
    base_url = request_input["base_url"].rstrip("/")
    requests = tuple(request_input["requests"])
    if not base_url or not requests:
        raise ValueError("http_get input needs a base_url and at least one request")
    wait_seconds = float(request_input.get("wait_seconds", metadata.load.wait_seconds))
    state = {
        "base_url": base_url,
        "requests": requests,
        "request_timeout": float(
            request_input.get(
                "request_timeout_seconds", metadata.load.request_timeout_seconds
            )
        ),
    }
    indices = itertools.count()
    completed = 0
    timed_out = False
    load_start_utc = None
    timeout = parse_timespan(metadata.load.max_duration)
    deadline = None
    cpu = None
    progress_greenlet = None

    def report_stats():
        if on_progress is None:
            return
        total = environment.stats.total
        on_progress(
            f"Locust: {total.num_requests}/{len(state['requests'])} requests, "
            f"{total.num_failures} failures, {runner.user_count} users, "
            f"{total.current_rps:.1f} req/s"
        )

    def report_periodically():
        while True:
            gevent.sleep(progress_interval_seconds)
            report_stats()

    class RequestUser(HttpUser):
        host = "http://localhost"
        wait_time = constant(wait_seconds)

        @task
        def fetch(self):
            requests = state["requests"]
            index = next(indices)
            if index >= len(requests):
                raise StopUser()
            item = requests[index]
            path = item["path"]
            with self.client.get(
                f"{state['base_url']}/{path}",
                name=item.get("name", "http_get"),
                context={"artifact_path": path},
                catch_response=True,
                timeout=state["request_timeout"],
            ) as response:
                expected = int(item.get("expected_status", 200))
                if response.status_code != expected:
                    response.failure(
                        f"HTTP {response.status_code}, expected {expected}: {path}"
                    )
                elif (
                    "sha256" in item
                    and hashlib.sha256(response.content).hexdigest() != item["sha256"]
                ):
                    response.failure(f"SHA256 mismatch: {path}")

    def stop_at_deadline():
        nonlocal timed_out
        timed_out = True
        if on_progress:
            on_progress("Load test timed out")
        runner.quit()

    with (output / "data_load.csv").open("w", newline="") as data_file:
        data_writer = csv.writer(data_file)
        data_writer.writerow(
            (
                "path",
                "url",
                "name",
                "expected_status",
                "sha256",
                "wait_seconds",
                "request_timeout_seconds",
            )
        )
        for item in requests:
            data_writer.writerow(
                (
                    item["path"],
                    f"{base_url}/{item['path']}",
                    item.get("name", "http_get"),
                    item.get("expected_status", 200),
                    item.get("sha256", ""),
                    wait_seconds,
                    state["request_timeout"],
                )
            )

    events = Events()
    environment = Environment(user_classes=[RequestUser], events=events)
    runner = environment.create_local_runner()
    cpu = (
        DBCpuMonitor(
            output / "db_cpu.csv",
            shlex.join(compose_command),
            project,
            str(compose_file) if compose_file else "compose.yaml",
        )
        if monitor_cpu
        else None
    )

    def count_request(request_type, **kwargs):
        nonlocal completed
        if request_type == "GET":
            completed += 1
            if completed >= len(state["requests"]):
                gevent.spawn(runner.quit)

    events.request.add_listener(count_request)

    with (output / "requests.csv").open("w", newline="") as request_file:
        request_writer = csv.writer(request_file)
        request_writer.writerow(
            (
                "started_at_utc",
                "completed_at_utc",
                "artifact_path",
                "url",
                "response_time_ms",
                "status_code",
                "response_bytes",
                "success",
                "error",
            )
        )

        def log_request(
            request_type,
            response_time,
            response,
            exception,
            start_time,
            url,
            context,
            response_length,
            **kwargs,
        ):
            if request_type != "GET":
                return
            request_writer.writerow(
                (
                    datetime.fromtimestamp(start_time, UTC).isoformat(
                        timespec="microseconds"
                    ),
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                    (context or {}).get("artifact_path", ""),
                    url,
                    f"{response_time:.3f}",
                    response.status_code if response is not None else "",
                    response_length,
                    exception is None,
                    str(exception) if exception is not None else "",
                )
            )

        events.request.add_listener(log_request)
        csv_writer = StatsCSVFileWriter(
            environment, PERCENTILES_TO_REPORT, str(output / "locust")
        )
        csv_greenlet = None
        try:
            if cpu:
                cpu.start()
            csv_greenlet = gevent.spawn(csv_writer.stats_writer)
            if on_progress:
                on_progress(
                    f"Locust: starting {metadata.load.users} users for {len(requests)} requests"
                )
            if metadata.load.idle_before_seconds:
                gevent.sleep(metadata.load.idle_before_seconds)
            deadline = gevent.spawn_later(timeout, stop_at_deadline)
            if on_progress:
                on_progress("Load test starting")
            load_start_utc = datetime.now(UTC).isoformat(timespec="microseconds")
            runner.start(metadata.load.users, metadata.load.spawn_rate_per_second)
            if on_progress:
                on_progress("Load test started")
            if on_progress:
                report_stats()
                progress_greenlet = gevent.spawn(report_periodically)
            runner.greenlet.join()
            if on_progress:
                on_progress("Load test stopped")
            deadline.kill()
            deadline = None
            report_stats()
            if metadata.load.tail_after_seconds:
                gevent.sleep(metadata.load.tail_after_seconds)
        finally:
            if deadline is not None:
                deadline.kill()
            if progress_greenlet is not None:
                progress_greenlet.kill(block=True)
            try:
                runner.quit()
            finally:
                try:
                    if csv_greenlet is not None:
                        csv_greenlet.kill(block=True)
                    csv_writer._stats_history_data_rows(
                        csv_writer.stats_history_csv_writer, time.time()
                    )
                finally:
                    try:
                        csv_writer.close_files()
                    finally:
                        if cpu:
                            cpu.stop()

    stats = StatsCSV(environment, PERCENTILES_TO_REPORT)
    for name, method in (
        ("locust_stats.csv", stats.requests_csv),
        ("locust_failures.csv", stats.failures_csv),
        ("locust_exceptions.csv", stats.exceptions_csv),
    ):
        with (output / name).open("w", newline="") as destination:
            method(csv.writer(destination))

    total = environment.stats.total
    result = RunResultMetadata(
        experiment_id=metadata.experiment_id,
        scenario_id=metadata.scenario_id,
        candidate_id=metadata.candidate.id,
        candidate_name=metadata.candidate.name,
        run_id=metadata.run_id,
        result_dir=output,
        expected_requests=len(state["requests"]),
        completed_requests=total.num_requests,
        failures=total.num_failures,
        mean_response_ms=total.avg_response_time,
        p95_response_ms=total.get_response_time_percentile(0.95),
        timed_out=timed_out,
        load_start_utc=load_start_utc,
    )
    return result
