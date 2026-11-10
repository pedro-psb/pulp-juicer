"""Build Maven request lists from the shared artifact fixture."""

import json
from collections.abc import Mapping

from pulp_juicer import ServiceFixtureMetadata, StepFunctionLoad, UnitaryImpulseLoad

LONG_STEP_UNIQUE_COUNT = 12_000
LONG_STEP_REPEAT_ROUNDS = 1


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


def build_pull_through_repeated(
    fixtures: Mapping[str, ServiceFixtureMetadata],
    candidate: dict,
    load: UnitaryImpulseLoad | StepFunctionLoad,
) -> dict:
    """Repeat the same 100 artifact requests in ten ordered blocks."""
    if load.request_count != 1000:
        raise ValueError("Repeated pull-through needs exactly 1,000 requests")
    fixture = fixtures["maven"]
    manifest = json.loads((fixture.output_dir / "manifest.json").read_text())
    artifacts = manifest["artifacts"][:100]
    if (
        len(artifacts) != 100
        or len({artifact["path"] for artifact in artifacts}) != 100
    ):
        raise ValueError("Repeated pull-through needs 100 unique fixture artifacts")
    return {
        "base_url": candidate["base_url"].rstrip("/"),
        "requests": [
            {
                "path": artifact["path"],
                "name": "bucket-repository",
                "expected_status": 200,
                "sha256": artifact["sha256"],
            }
            for _ in range(10)
            for artifact in artifacts
        ],
        "wait_seconds": load.wait_seconds,
        "request_timeout_seconds": load.request_timeout_seconds,
    }


def build_pull_through_unique_then_repeated(
    fixtures: Mapping[str, ServiceFixtureMetadata],
    candidate: dict,
    load: StepFunctionLoad,
) -> dict:
    """Request unique artifacts first, then repeat each one once."""
    expected_count = LONG_STEP_UNIQUE_COUNT * (1 + LONG_STEP_REPEAT_ROUNDS)
    if load.request_count != expected_count:
        raise ValueError(
            f"Unique/repeated pull-through needs {expected_count} requests"
        )
    fixture = fixtures["maven"]
    manifest = json.loads((fixture.output_dir / "manifest.json").read_text())
    artifacts = manifest["artifacts"][:LONG_STEP_UNIQUE_COUNT]
    if (
        len(artifacts) != LONG_STEP_UNIQUE_COUNT
        or len({artifact["path"] for artifact in artifacts}) != LONG_STEP_UNIQUE_COUNT
    ):
        raise ValueError(
            f"Unique/repeated pull-through needs {LONG_STEP_UNIQUE_COUNT} unique artifacts"
        )
    return {
        "base_url": candidate["base_url"].rstrip("/"),
        "requests": [
            {
                "path": artifact["path"],
                "name": "bucket-repository",
                "expected_status": 200,
                "sha256": artifact["sha256"],
            }
            for _ in range(1 + LONG_STEP_REPEAT_ROUNDS)
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
        "commit_staging_repository": (
            candidate["repository"] if candidate["staging_enabled"] else None
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
