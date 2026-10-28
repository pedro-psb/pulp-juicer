"""Request each Maven artifact once through a Pulp pull-through distribution."""

import hashlib
import itertools
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from locust import HttpUser, constant, task
from locust.exception import StopUser


mode = os.environ.get("MODE", "staging")
if mode not in {"staging", "normal"}:
    raise ValueError("MODE must be staging or normal")

config = json.loads(Path(os.environ.get("SETUP_JSON", "setup.json")).read_text())
manifest = json.loads(Path(os.environ.get("MANIFEST_JSON", "fixture/manifest.json")).read_text())
artifacts = manifest[mode]
indices = itertools.count()
distribution_url = config[mode]["base_url"].rstrip("/")
if origin := os.environ.get("CONTENT_ORIGIN"):
    distribution_url = origin.rstrip("/") + urlsplit(distribution_url).path.rstrip("/")


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
            name=f"pull-through/{mode}",
            catch_response=True,
            timeout=60,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {artifact['path']}")
            elif hashlib.sha256(response.content).hexdigest() != artifact["sha256"]:
                response.failure(f"SHA256 mismatch: {artifact['path']}")

