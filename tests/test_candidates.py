"""Maven candidate setup payloads for different Pulp versions."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from experiments.maven_pull_through.candidates import (
    NormalCandidate,
    StagingCandidate,
    StagingManualCandidate,
)
from pulp_juicer.types import PulpConnection, ServiceFixtureMetadata


class TestMavenCandidateSetup(unittest.TestCase):
    def test_repository_fields_match_candidate(self):
        fixture = ServiceFixtureMetadata(
            name="maven",
            compose_file=Path("compose.yaml"),
            service_name="maven",
            output_dir=Path("fixture"),
            url="http://localhost:8080",
        )
        cases = (
            (NormalCandidate, {"name": "loadtest-test"}),
            (
                StagingCandidate,
                {
                    "name": "loadtest-test",
                    "staging_enabled": True,
                    "staging_autocommit": True,
                },
            ),
            (
                StagingManualCandidate,
                {
                    "name": "loadtest-test",
                    "staging_enabled": True,
                    "staging_autocommit": False,
                },
            ),
        )
        for candidate_type, expected_payload in cases:
            with self.subTest(candidate=candidate_type.__name__):
                client = MagicMock()
                client.wait_for_created_resource.side_effect = (
                    {"pulp_href": "/remote/"},
                    {"pulp_href": "/repository/"},
                    {"pulp_href": "/distribution/", "base_url": "http://example/"},
                )
                with patch(
                    "experiments.maven_pull_through.candidates.PulpClient",
                    return_value=client,
                ):
                    candidate_type(id=1, name="test", sources={}).setup(
                        PulpConnection(), {"maven": fixture}, "test"
                    )
                repository_calls = [
                    call
                    for call in client.post.call_args_list
                    if call.args[0] == "/pulp/api/v3/repositories/maven/maven/"
                ]
                self.assertEqual(len(repository_calls), 1)
                self.assertEqual(repository_calls[0].args[1], expected_payload)
