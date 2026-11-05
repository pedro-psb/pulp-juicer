"""Maven candidate setup and Pulp settings for different implementations."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from experiments.bucket_repository.experiment import EXPERIMENT
from pulp_juicer.execution import _compose_inputs
from pulp_juicer.plan import build_test_plan
from pulp_juicer.types import PulpConnection, ServiceFixtureMetadata


class TestMavenCandidateSetup(unittest.TestCase):
    def test_staging_does_not_add_autocommit_settings(self):
        compose = (
            Path(__file__).resolve().parents[1]
            / "src/experiments/bucket_repository/compose.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=Path(tmp),
                scenario_id="upload-impulse",
            )
            normal = next(run for run in plan.runs if run.candidate.name == "normal")
            staging = next(run for run in plan.runs if run.candidate.name == "staging")
            normal_files, _ = _compose_inputs(plan, normal)
            staging_files, _ = _compose_inputs(plan, staging)
            self.assertEqual(len(staging_files), len(normal_files))
            self.assertEqual(staging.candidate.pulp_settings, {})

    def test_repository_fields_match_candidate(self):
        fixture = ServiceFixtureMetadata(
            name="maven",
            compose_file=Path("compose.yaml"),
            service_name="maven",
            output_dir=Path("fixture"),
            url="http://localhost:8080",
        )
        cases = (
            (
                EXPERIMENT.candidates[0].pre_test_functions[0].setup_fn,
                {"name": "loadtest-test"},
            ),
            (
                EXPERIMENT.candidates[1].pre_test_functions[0].setup_fn,
                {
                    "name": "loadtest-test",
                    "staging_enabled": True,
                    "staging_autocommit": False,
                },
            ),
        )
        for setup_fn, expected_payload in cases:
            with self.subTest(expected_payload=expected_payload):
                client = MagicMock()
                client.wait_for_created_resource.side_effect = (
                    {"pulp_href": "/remote/"},
                    {"pulp_href": "/repository/"},
                    {"pulp_href": "/distribution/", "base_url": "http://example/"},
                )
                with patch(
                    "experiments.bucket_repository.candidates.PulpClient",
                    return_value=client,
                ):
                    setup_fn(PulpConnection(), {"maven": fixture}, "test")
                repository_calls = [
                    call
                    for call in client.post.call_args_list
                    if call.args[0] == "/pulp/api/v3/repositories/maven/maven/"
                ]
                self.assertEqual(len(repository_calls), 1)
                self.assertEqual(repository_calls[0].args[1], expected_payload)
