"""Set up Maven repository variants using a running Pulp instance."""

import re

from pulp_juicer import (
    CandidateConfig,
    PulpClient,
    PulpConnection,
    ServiceFixtureMetadata,
)


def setup_maven(
    connection: PulpConnection,
    fixture: ServiceFixtureMetadata,
    run_id: str,
    *,
    staging_enabled: bool,
    staging_autocommit: bool,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(
            "Run ID must contain only letters, numbers, underscores, or hyphens"
        )
    client = PulpClient(connection)
    name = f"loadtest-{run_id}"
    remote = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/remotes/maven/maven/",
            {"name": f"{name}-remote", "url": fixture.url.rstrip("/") + "/"},
        )
    )["pulp_href"]
    repository = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/repositories/maven/maven/",
            {
                "name": name,
                "staging_enabled": staging_enabled,
                "staging_autocommit": staging_autocommit,
            },
        )
    )["pulp_href"]
    distribution = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/distributions/maven/maven/",
            {
                "name": name,
                "base_path": name,
                "remote": remote,
                "repository": repository,
            },
        )
    )
    return {
        "base_url": distribution["base_url"],
        "repository": repository,
        "distribution": distribution["pulp_href"],
        "remote": remote,
    }


class NormalCandidate(CandidateConfig):
    def setup(
        self,
        connection: PulpConnection,
        fixtures: dict[str, ServiceFixtureMetadata],
        run_id: str,
    ) -> dict:
        return setup_maven(
            connection,
            fixtures["maven"],
            run_id,
            staging_enabled=False,
            staging_autocommit=False,
        )


class StagingCandidate(CandidateConfig):
    def setup(
        self,
        connection: PulpConnection,
        fixtures: dict[str, ServiceFixtureMetadata],
        run_id: str,
    ) -> dict:
        return setup_maven(
            connection,
            fixtures["maven"],
            run_id,
            staging_enabled=True,
            staging_autocommit=True,
        )


class StagingManualCandidate(CandidateConfig):
    def setup(
        self,
        connection: PulpConnection,
        fixtures: dict[str, ServiceFixtureMetadata],
        run_id: str,
    ) -> dict:
        return setup_maven(
            connection,
            fixtures["maven"],
            run_id,
            staging_enabled=True,
            staging_autocommit=False,
        )
