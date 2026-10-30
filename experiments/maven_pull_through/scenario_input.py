"""Build a serializable request specification from setup artifacts."""

from pulp_juicer import LoadConfig


def build_pull_through(fixture: dict, run_fixture: dict, candidate: dict, load: LoadConfig) -> dict:
    available = run_fixture["artifacts"]
    capacity = int(fixture["artifacts_per_run"])
    request_count = capacity if load.request_count is None else load.request_count
    if request_count < 1 or request_count > capacity or request_count > len(available):
        raise ValueError("request_count must be between 1 and the available fixture count")
    artifacts = available[:request_count]
    if len({artifact["path"] for artifact in artifacts}) != request_count:
        raise ValueError("Selected fixture artifacts must have unique paths")
    return {
        "base_url": candidate["base_url"],
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
