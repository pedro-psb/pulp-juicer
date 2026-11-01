"""Set up Maven repository variants using a running Pulp instance."""

import re
from collections.abc import Mapping

from pulp_juicer import (
    PulpClient,
    PulpConnection,
    ServiceFixtureMetadata,
)


def _setup_maven(
    connection: PulpConnection,
    fixtures: Mapping[str, ServiceFixtureMetadata],
    run_id: str,
    repository_options: Mapping[str, bool],
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(
            "Run ID must contain only letters, numbers, underscores, or hyphens"
        )
    client = PulpClient(connection)
    fixture = fixtures["maven"]
    name = f"loadtest-{run_id}"
    remote = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/remotes/maven/maven/",
            {"name": f"{name}-remote", "url": fixture.url.rstrip("/") + "/"},
        )
    )["pulp_href"]
    repository_payload = {"name": name, **repository_options}
    repository = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/repositories/maven/maven/",
            repository_payload,
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


def setup_normal(
    connection: PulpConnection,
    fixtures: Mapping[str, ServiceFixtureMetadata],
    run_id: str,
) -> dict:
    return _setup_maven(connection, fixtures, run_id, {})


def setup_staging(
    connection: PulpConnection,
    fixtures: Mapping[str, ServiceFixtureMetadata],
    run_id: str,
) -> dict:
    return _setup_maven(
        connection,
        fixtures,
        run_id,
        {"staging_enabled": True, "staging_autocommit": True},
    )
