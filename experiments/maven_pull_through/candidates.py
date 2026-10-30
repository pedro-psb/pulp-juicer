"""Set up Maven repository variants using a running Pulp instance."""

import re

from pulp_juicer import CandidateConfig, PulpClient, PulpConnection



def setup_maven(connection: PulpConnection, fixture: dict, run_id: str, *, staging_enabled: bool, staging_autocommit: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Run ID must contain only letters, numbers, underscores, or hyphens")
    client = PulpClient(connection)
    name = f"loadtest-{run_id}"
    remote = client.create(
        "/pulp/api/v3/remotes/maven/maven/",
        {"name": f"{name}-remote", "url": fixture["upstream_url"].rstrip("/") + "/"},
    )["pulp_href"]
    repository = client.create(
        "/pulp/api/v3/repositories/maven/maven/",
        {"name": name, "staging_enabled": staging_enabled, "staging_autocommit": staging_autocommit},
    )["pulp_href"]
    distribution = client.create(
        "/pulp/api/v3/distributions/maven/maven/",
        {"name": name, "base_path": name, "remote": remote, "repository": repository},
    )
    return {
        "base_url": distribution["base_url"],
        "repository": repository,
        "distribution": distribution["pulp_href"],
        "remote": remote,
    }


class NormalCandidate(CandidateConfig):
    def setup(self, connection: PulpConnection, fixture_context: dict, run_id: str) -> dict:
        return setup_maven(
            connection, fixture_context, run_id, staging_enabled=False, staging_autocommit=False
        )


class StagingCandidate(CandidateConfig):
    def setup(self, connection: PulpConnection, fixture_context: dict, run_id: str) -> dict:
        return setup_maven(
            connection, fixture_context, run_id, staging_enabled=True, staging_autocommit=True
        )


class StagingManualCandidate(CandidateConfig):
    def setup(self, connection: PulpConnection, fixture_context: dict, run_id: str) -> dict:
        return setup_maven(
            connection, fixture_context, run_id, staging_enabled=True, staging_autocommit=False
        )
