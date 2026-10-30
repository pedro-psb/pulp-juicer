"""Run one HTTP GET measurement with Locust's Python API."""

import csv
import hashlib
import itertools
import shlex
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import gevent
from gevent.event import Event
from locust import HttpUser, constant, task
from locust.env import Environment
from locust.event import Events
from locust.exception import StopUser
from locust.stats import PERCENTILES_TO_REPORT, StatsCSV, StatsCSVFileWriter
from locust.util.timespan import parse_timespan

from .db_cpu_monitor import DBCpuMonitor
from .plan import RunMetadata, RunResultMetadata


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
    """Execute one run and retain its artifacts only after full success."""
    if metadata.driver != "http_get":
        raise NotImplementedError(f"Unsupported driver: {metadata.driver}")

    output = metadata.result_dir
    output.mkdir(parents=True, exist_ok=False)
    successful = False
    try:
        result = _run_created(
            metadata, on_start, on_stop, compose_command=compose_command,
            project=project, compose_file=compose_file, monitor_cpu=monitor_cpu,
            on_progress=on_progress, progress_interval_seconds=progress_interval_seconds,
        )
        successful = (
            not result.timed_out and result.failures == 0
            and result.completed_requests >= result.expected_requests
        )
        return result
    finally:
        if not successful:
            shutil.rmtree(output)


def _run_created(
    metadata: RunMetadata,
    on_start: Callable[[], dict],
    on_stop: Callable[[], None],
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
    state = {"base_url": "", "requests": (), "started": False, "start_error": None, "stop_error": None}
    stop_done = Event()
    indices = itertools.count()
    completed = 0
    timed_out = False
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
        wait_time = constant(metadata.load.wait_seconds)

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
                    response.failure(f"HTTP {response.status_code}, expected {expected}: {path}")
                elif "sha256" in item and hashlib.sha256(response.content).hexdigest() != item["sha256"]:
                    response.failure(f"SHA256 mismatch: {path}")

    events = Events()
    environment = Environment(user_classes=[RequestUser], events=events)
    runner = environment.create_local_runner()

    def stop_at_deadline():
        nonlocal timed_out
        timed_out = True
        runner.quit()

    def start_test(environment, **kwargs):
        nonlocal deadline, cpu
        try:
            if on_progress:
                on_progress("Locust: preparing service and requests...")
            request_input = on_start()
            base_url = request_input["base_url"].rstrip("/")
            requests = tuple(request_input["requests"])
            if not base_url or not requests:
                raise ValueError("http_get input needs a base_url and at least one request")
            state.update({
                "base_url": base_url, "requests": requests,
                "request_timeout": float(request_input.get("request_timeout_seconds", metadata.load.request_timeout_seconds)),
            })
            wait_seconds = float(request_input.get("wait_seconds", metadata.load.wait_seconds))
            RequestUser.wait_time = constant(wait_seconds)
            with (output / "data_load.csv").open("w", newline="") as data_file:
                data_writer = csv.writer(data_file)
                data_writer.writerow((
                    "path", "url", "name", "expected_status", "sha256",
                    "wait_seconds", "request_timeout_seconds",
                ))
                for item in requests:
                    data_writer.writerow((
                        item["path"], f"{base_url}/{item['path']}", item.get("name", "http_get"),
                        item.get("expected_status", 200), item.get("sha256", ""),
                        wait_seconds, state["request_timeout"],
                    ))
            cpu = DBCpuMonitor(
                output / "db_cpu.csv", shlex.join(compose_command), project,
                str(compose_file) if compose_file else "compose.yaml",
            ) if monitor_cpu else None
            if cpu:
                cpu.start()
            deadline = gevent.spawn_later(timeout, stop_at_deadline)
            state["started"] = True
            if on_progress:
                on_progress(f"Locust: starting {metadata.load.users} users for {len(requests)} requests")
            if metadata.load.idle_before_seconds:
                gevent.sleep(metadata.load.idle_before_seconds)
        except Exception as exc:
            state["start_error"] = exc

    def stop_test(environment, **kwargs):
        try:
            if deadline:
                deadline.kill()
            if state["started"] and metadata.load.tail_after_seconds:
                gevent.sleep(metadata.load.tail_after_seconds)
            if cpu:
                cpu.stop()
        except Exception as exc:
            state["stop_error"] = exc
        finally:
            try:
                on_stop()
            except Exception as exc:
                state["stop_error"] = exc
            stop_done.set()

    events.test_start.add_listener(start_test)
    events.test_stop.add_listener(stop_test)

    def count_request(request_type, **kwargs):
        nonlocal completed
        if request_type == "GET":
            completed += 1
            if completed >= len(state["requests"]):
                gevent.spawn(runner.quit)

    events.request.add_listener(count_request)

    with (output / "requests.csv").open("w", newline="") as request_file:
        request_writer = csv.writer(request_file)
        request_writer.writerow((
            "started_at_utc", "completed_at_utc", "artifact_path", "url",
            "response_time_ms", "status_code", "response_bytes", "success", "error",
        ))

        def log_request(request_type, response_time, response, exception, start_time, url, context,
                        response_length, **kwargs):
            if request_type != "GET":
                return
            request_writer.writerow((
                datetime.fromtimestamp(start_time, timezone.utc).isoformat(timespec="microseconds"),
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                (context or {}).get("artifact_path", ""), url, f"{response_time:.3f}",
                response.status_code if response is not None else "", response_length,
                exception is None, str(exception) if exception is not None else "",
            ))

        events.request.add_listener(log_request)
        csv_writer = StatsCSVFileWriter(environment, PERCENTILES_TO_REPORT, str(output / "locust"))
        csv_greenlet = gevent.spawn(csv_writer.stats_writer)
        try:
            runner.start(metadata.load.users, metadata.load.spawn_rate_per_second)
            while not state["started"] and state["start_error"] is None:
                if runner.spawning_greenlet.ready():
                    raise RuntimeError("Locust stopped before the start hook completed")
                gevent.sleep(0.05)
            if state["start_error"] is not None:
                raise state["start_error"]
            if on_progress:
                report_stats()
                progress_greenlet = gevent.spawn(report_periodically)
            runner.greenlet.join()
            stop_done.wait()
            report_stats()
            if state["stop_error"] is not None:
                raise state["stop_error"]
        finally:
            if progress_greenlet is not None:
                progress_greenlet.kill(block=True)
            runner.quit()
            stop_done.wait()
            csv_greenlet.kill(block=True)
            csv_writer._stats_history_data_rows(csv_writer.stats_history_csv_writer, time.time())
            csv_writer.close_files()

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
    )
    return result
