"""Request each Maven artifact once through a Pulp pull-through distribution."""

import csv
import hashlib
import itertools
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.stats import update_stats_history

from db_cpu_monitor import DBCpuMonitor


candidate = os.environ.get("CANDIDATE", "normal")
if candidate not in {"staging", "normal"}:
    raise ValueError("CANDIDATE must be staging or normal")

run_dir = Path("_results") / "_run" / os.environ.get("RUN_ID", "local001")
config = json.loads(Path(os.environ.get("SETUP_JSON", run_dir / "setup.json")).read_text())
manifest = json.loads(Path(os.environ.get("MANIFEST_JSON", "fixture/manifest.json")).read_text())
artifacts = manifest[candidate]
indices = itertools.count()
distribution_url = config[candidate]["base_url"].rstrip("/")
if origin := os.environ.get("CONTENT_ORIGIN"):
    distribution_url = origin.rstrip("/") + urlsplit(distribution_url).path.rstrip("/")

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
    if request_writer is None or request_type != "GET" or name != f"pull-through/{candidate}":
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
        if request_type != "GET" or name != f"pull-through/{candidate}":
            return
        completed += 1
        if completed == len(artifacts):
            gevent.spawn(environment.runner.quit)


class PullThroughUser(HttpUser):
    host = distribution_url
    wait_time = constant(float(os.environ.get("WAIT_SECONDS", "0")))

    @task
    def fetch_artifact(self):
        index = next(indices)
        if index >= len(artifacts):
            raise StopUser()

        artifact = artifacts[index]
        with self.client.get(
            f"{distribution_url}/{artifact['path']}",
            name=f"pull-through/{candidate}",
            context={"artifact_path": artifact["path"]},
            catch_response=True,
            timeout=60,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {artifact['path']}")
            elif hashlib.sha256(response.content).hexdigest() != artifact["sha256"]:
                response.failure(f"SHA256 mismatch: {artifact['path']}")

