"""Request each Maven artifact once through a Pulp pull-through distribution."""

import csv
import hashlib
import itertools
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.stats import update_stats_history

from pulp_juicer.db_cpu_monitor import DBCpuMonitor


@dataclass(frozen=True)
class RequestInput:
    path: str
    sha256: str
    expected_status: int
    name: str


@dataclass(frozen=True)
class ScenarioInput:
    candidate: str
    base_url: str
    requests: tuple[RequestInput, ...]
    wait_seconds: float
    request_timeout_seconds: float


def _scenario_input_from_json(path: Path) -> ScenarioInput:
    raw = json.loads(path.read_text())
    candidate_name = raw.get("candidate", os.environ.get("CANDIDATE", "custom"))
    requests = tuple(
        RequestInput(
            path=request["path"],
            sha256=request["sha256"],
            expected_status=int(request.get("expected_status", 200)),
            name=request.get("name", f"pull-through/{candidate_name}"),
        )
        for request in raw["requests"]
    )
    return ScenarioInput(
        candidate=candidate_name,
        base_url=raw["base_url"].rstrip("/"),
        requests=requests,
        wait_seconds=float(raw.get("wait_seconds", 0)),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 60)),
    )


def build_scenario_input(
    setup: dict,
    manifest: dict,
    candidate: str,
    *,
    content_origin: str | None = None,
    wait_seconds: float = 0,
    request_timeout_seconds: float = 60,
) -> ScenarioInput:
    distribution_url = setup[candidate]["base_url"].rstrip("/")
    if content_origin:
        distribution_url = content_origin.rstrip("/") + urlsplit(distribution_url).path.rstrip("/")
    requests = tuple(
        RequestInput(
            path=artifact["path"],
            sha256=artifact["sha256"],
            expected_status=200,
            name=f"pull-through/{candidate}",
        )
        for artifact in manifest[candidate]
    )
    return ScenarioInput(
        candidate=candidate,
        base_url=distribution_url,
        requests=requests,
        wait_seconds=wait_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


candidate = os.environ.get("CANDIDATE", "normal")
scenario_input_path = os.environ.get("SCENARIO_INPUT_JSON")
if scenario_input_path:
    scenario_input = _scenario_input_from_json(Path(scenario_input_path))
else:
    if candidate not in {"staging", "normal"}:
        raise ValueError("CANDIDATE must be staging or normal")
    run_dir = Path("_results") / "_run" / os.environ.get("RUN_ID", "local001")
    config = json.loads(Path(os.environ.get("SETUP_JSON", run_dir / "setup.json")).read_text())
    manifest = json.loads(Path(os.environ.get("MANIFEST_JSON", "fixture/manifest.json")).read_text())
    scenario_input = build_scenario_input(
        setup=config,
        manifest=manifest,
        candidate=candidate,
        content_origin=os.environ.get("CONTENT_ORIGIN"),
        wait_seconds=float(os.environ.get("WAIT_SECONDS", "0")),
    )
request_names = {request.name for request in scenario_input.requests}
indices = itertools.count()

db_cpu_monitor = None
request_log = None
request_writer = None
idle_seconds = float(os.environ.get("IDLE_SECONDS", "5"))
tail_seconds = float(os.environ.get("TAIL_SECONDS", "5"))
if any(not math.isfinite(value) or value < 0 for value in (idle_seconds, tail_seconds)):
    raise ValueError("IDLE_SECONDS and TAIL_SECONDS must be non-negative finite numbers")


@events.test_start.add_listener
def start_request_log(environment, **kwargs):
    global request_log, request_writer
    if output := os.environ.get("REQUESTS_CSV"):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        request_log = path.open("w", newline="")
        request_writer = csv.writer(request_log)
        request_writer.writerow((
            "started_at_utc", "completed_at_utc", "artifact_path", "url",
            "response_time_ms", "status_code", "response_bytes", "success", "error",
        ))


@events.request.add_listener
def record_request(request_type, name, response_time, response, exception, start_time, url, context, response_length, **kwargs):
    if request_writer is None or request_type != "GET" or name not in request_names:
        return
    request_writer.writerow((
        datetime.fromtimestamp(start_time, timezone.utc).isoformat(timespec="microseconds"),
        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        (context or {}).get("artifact_path", ""),
        url,
        f"{response_time:.3f}",
        response.status_code if response is not None else "",
        response_length,
        exception is None,
        str(exception) if exception is not None else "",
    ))


@events.test_stop.add_listener
def stop_request_log(environment, **kwargs):
    global request_log, request_writer
    if request_log is not None:
        request_log.close()
        request_log = None
        request_writer = None


@events.test_start.add_listener
def start_db_cpu(environment, **kwargs):
    global db_cpu_monitor
    if output := os.environ.get("DB_CPU_CSV"):
        db_cpu_monitor = DBCpuMonitor(
            output,
            os.environ.get("DB_CPU_COMPOSE", "docker compose"),
            os.environ.get("DB_CPU_PROJECT", "bucket-loadtest"),
            os.environ.get("DB_CPU_COMPOSE_FILE", str(Path(__file__).with_name("compose.yaml"))),
        )
        try:
            db_cpu_monitor.start()
        except Exception:
            environment.process_exit_code = 2
            gevent.spawn(environment.runner.quit)
            raise
    if idle_seconds:
        update_stats_history(environment.runner)
        gevent.sleep(idle_seconds)
        update_stats_history(environment.runner)


@events.test_stop.add_listener
def stop_db_cpu(environment, **kwargs):
    global db_cpu_monitor
    if tail_seconds and environment.process_exit_code in (None, 0):
        update_stats_history(environment.runner)
        gevent.sleep(tail_seconds)
        update_stats_history(environment.runner)
    if db_cpu_monitor is not None:
        try:
            db_cpu_monitor.stop()
        except Exception:
            environment.process_exit_code = 2
            raise
        finally:
            db_cpu_monitor = None


@events.init.add_listener
def quit_when_complete(environment, **kwargs):
    completed = 0

    @environment.events.request.add_listener
    def count_request(request_type, name, **kwargs):
        nonlocal completed
        if request_type != "GET" or name not in request_names:
            return
        completed += 1
        if completed == len(scenario_input.requests):
            gevent.spawn(environment.runner.quit)


class PullThroughUser(HttpUser):
    host = scenario_input.base_url
    wait_time = constant(scenario_input.wait_seconds)

    @task
    def fetch_artifact(self):
        index = next(indices)
        if index >= len(scenario_input.requests):
            raise StopUser()

        request_input = scenario_input.requests[index]
        with self.client.get(
            f"{scenario_input.base_url}/{request_input.path}",
            name=request_input.name,
            context={"artifact_path": request_input.path},
            catch_response=True,
            timeout=scenario_input.request_timeout_seconds,
        ) as response:
            if response.status_code != request_input.expected_status:
                response.failure(
                    f"HTTP {response.status_code}, expected {request_input.expected_status}: {request_input.path}"
                )
            elif hashlib.sha256(response.content).hexdigest() != request_input.sha256:
                response.failure(f"SHA256 mismatch: {request_input.path}")

