"""Build the Maven pull-through request list from its required fixture."""

import json
from collections.abc import Mapping

from pulp_juicer import LoadConfig, ServiceFixtureMetadata


def build_pull_through(
    fixtures: Mapping[str, ServiceFixtureMetadata], candidate: dict, load: LoadConfig
) -> dict:
    fixture = fixtures["maven"]
    manifest = json.loads((fixture.output_dir / "manifest.json").read_text())
    available = manifest["artifacts"]
    request_count = len(available) if load.request_count is None else load.request_count
    if request_count < 1 or request_count > len(available):
        raise ValueError(
            "request_count must be between 1 and the available fixture count"
        )
    artifacts = available[:request_count]
    if len({artifact["path"] for artifact in artifacts}) != request_count:
        raise ValueError("Selected fixture artifacts must have unique paths")
    return {
        "base_url": candidate["base_url"].rstrip("/"),
        "requests": [
            {
                "path": artifact["path"],
                "name": "maven-pull-through",
                "expected_status": 200,
                "sha256": artifact["sha256"],
            }
            for artifact in artifacts
        ],
        "wait_seconds": load.wait_seconds,
        "request_timeout_seconds": load.request_timeout_seconds,
    }
