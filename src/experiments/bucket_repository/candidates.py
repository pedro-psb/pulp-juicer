"""Set up Maven repository variants using a running Pulp instance."""

import re
from collections.abc import Mapping

from pulp_juicer import (
    PulpClient,
    PulpConnection,
    PulpPreTestFn,
    FixtureMetadata,
    ServiceFixtureMetadata,
)


def _setup_maven(
    connection: PulpConnection,
    fixtures: Mapping[str, FixtureMetadata],
    run_id: str,
    repository_options: Mapping[str, bool],
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(
            "Run ID must contain only letters, numbers, underscores, or hyphens"
        )
    client = PulpClient(connection)
    fixture = fixtures["maven"]
    if not isinstance(fixture, ServiceFixtureMetadata):
        raise TypeError("Maven fixture must be a service fixture")
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
        "api_base_url": connection.base_url,
        "repository": repository,
        "staging_enabled": bool(repository_options.get("staging_enabled")),
        "distribution": distribution["pulp_href"],
        "remote": remote,
    }


class NormalCandidate:
    @staticmethod
    def setup() -> PulpPreTestFn:
        """Return the standard Maven repository preparation callback."""

        def pre_test_fn(
            connection: PulpConnection,
            fixtures: Mapping[str, FixtureMetadata],
            run_id: str,
        ) -> dict:
            return _setup_maven(connection, fixtures, run_id, {})

        return pre_test_fn


class StagingCandidate:
    @staticmethod
    def setup(*, autocommit: bool) -> PulpPreTestFn:
        """Return a preparation callback for the selected commit policy."""

        def pre_test_fn(
            connection: PulpConnection,
            fixtures: Mapping[str, FixtureMetadata],
            run_id: str,
        ) -> dict:
            return _setup_maven(
                connection,
                fixtures,
                run_id,
                {"staging_enabled": True, "staging_autocommit": autocommit},
            )

        return pre_test_fn
