"""Build Maven request lists from the shared artifact fixture."""

import json
from collections.abc import Mapping

from pulp_juicer import ServiceFixtureMetadata, StepFunctionLoad, UnitaryImpulseLoad


def build_pull_through(
    fixtures: Mapping[str, ServiceFixtureMetadata],
    candidate: dict,
    load: UnitaryImpulseLoad | StepFunctionLoad,
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
                "name": "bucket-repository",
                "expected_status": 200,
                "sha256": artifact["sha256"],
            }
            for artifact in artifacts
        ],
        "wait_seconds": load.wait_seconds,
        "request_timeout_seconds": load.request_timeout_seconds,
    }


def build_upload(
    fixtures: Mapping[str, ServiceFixtureMetadata],
    candidate: dict,
    load: UnitaryImpulseLoad | StepFunctionLoad,
) -> dict:
    """Upload each generated JAR to the candidate's Maven repository."""
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
        "base_url": candidate["api_base_url"].rstrip("/"),
        "modify_repository": (
            None if candidate["staging_enabled"] else candidate["repository"]
        ),
        "requests": [
            {
                "path": "pulp/api/v3/content/maven/artifact/upload/",
                "name": "maven-upload",
                "expected_status": 201,
                "relative_path": artifact["path"],
                "file_path": str(fixture.output_dir / artifact["path"]),
                **(
                    {"repository": candidate["repository"]}
                    if candidate["staging_enabled"]
                    else {}
                ),
            }
            for artifact in artifacts
        ],
        "wait_seconds": load.wait_seconds,
        "request_timeout_seconds": load.request_timeout_seconds,
    }
