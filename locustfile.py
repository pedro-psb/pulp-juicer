"""Request each Maven artifact once through a Pulp pull-through distribution."""

import hashlib
import itertools
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, constant, events, task
from locust.exception import StopUser

from db_cpu_monitor import DBCpuMonitor


if "MODE" in os.environ:
    raise ValueError("MODE was renamed to SCENARIO")
scenario = os.environ.get("SCENARIO", "normal")
if scenario not in {"staging", "normal"}:
    raise ValueError("SCENARIO must be staging or normal")

run_dir = Path("_results") / "_run" / os.environ.get("RUN_ID", "local001")
config = json.loads(Path(os.environ.get("SETUP_JSON", run_dir / "setup.json")).read_text())
manifest = json.loads(Path(os.environ.get("MANIFEST_JSON", "fixture/manifest.json")).read_text())
artifacts = manifest[scenario]
indices = itertools.count()
distribution_url = config[scenario]["base_url"].rstrip("/")
if origin := os.environ.get("CONTENT_ORIGIN"):
    distribution_url = origin.rstrip("/") + urlsplit(distribution_url).path.rstrip("/")

db_cpu_monitor = None


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


@events.test_stop.add_listener
def stop_db_cpu(environment, **kwargs):
    global db_cpu_monitor
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
        if request_type != "GET" or name != f"pull-through/{scenario}":
            return
        completed += 1
        if completed == len(artifacts):
            # Locust writes CSV snapshots once a second; keep the final sample.
            gevent.spawn_later(1.1, environment.runner.quit)


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
            name=f"pull-through/{scenario}",
            catch_response=True,
            timeout=60,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {artifact['path']}")
            elif hashlib.sha256(response.content).hexdigest() != artifact["sha256"]:
                response.failure(f"SHA256 mismatch: {artifact['path']}")

