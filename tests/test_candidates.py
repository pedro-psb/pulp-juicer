"""Maven candidate setup and Pulp settings for different implementations."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from experiments.bucket_repository.experiment import EXPERIMENT
from pulp_juicer.execution import _compose_inputs
from pulp_juicer.plan import build_test_plan
from pulp_juicer.plan_file import load_plan_file, write_plan_file
from pulp_juicer.types import PulpConnection, ServiceFixtureMetadata


class TestMavenCandidateSetup(unittest.TestCase):
    def test_scenario_preparation_uses_candidate_names(self):
        for scenario in EXPERIMENT.scenarios:
            self.assertEqual(set(scenario.candidate_config), {"normal", "staging"})
        with self.assertRaisesRegex(ValueError, "Candidate names must be unique"):
            replace(
                EXPERIMENT,
                candidates=(
                    EXPERIMENT.candidates[0],
                    replace(EXPERIMENT.candidates[1], name="normal"),
                ),
            )

    def test_plan_restores_scenario_candidate_preparation(self):
        compose = (
            Path(__file__).resolve().parents[1]
            / "src/experiments/bucket_repository/compose.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=Path(tmp),
                scenario_id="pull-through-step-30-users-unique-repeated",
            )
            restored = load_plan_file(write_plan_file(plan))
            self.assertEqual(
                [run.pulp_prepare.settings for run in restored.runs],
                [run.pulp_prepare.settings for run in plan.runs],
            )
            self.assertIs(
                restored.runs[1].pulp_prepare.pre_test_fn,
                plan.runs[1].pulp_prepare.pre_test_fn,
            )

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
            self.assertEqual(staging.pulp_prepare.settings, {})
            pull_plan = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=Path(tmp),
                scenario_id="pull-through-step-30-users-unique-repeated",
            )
            pull_staging = next(
                run for run in pull_plan.runs if run.candidate.name == "staging"
            )
            pull_files, _ = _compose_inputs(pull_plan, pull_staging)
            self.assertEqual(len(pull_files), len(normal_files) + 1)
            self.assertEqual(
                pull_staging.pulp_prepare.settings,
                {
                    "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                    "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                },
            )

    def test_repository_fields_match_candidate(self):
        fixture = ServiceFixtureMetadata(
            name="maven",
            compose_file=Path("compose.yaml"),
            service_name="maven",
            output_dir=Path("fixture"),
            url="http://localhost:8080",
        )
        scenarios = {scenario.id: scenario for scenario in EXPERIMENT.scenarios}
        cases = (
            (
                scenarios["upload-impulse"].candidate_config["normal"].pre_test_fn,
                {"name": "loadtest-test"},
            ),
            (
                scenarios["upload-impulse"].candidate_config["staging"].pre_test_fn,
                {
                    "name": "loadtest-test",
                    "staging_enabled": True,
                    "staging_autocommit": False,
                },
            ),
            (
                scenarios["pull-through-step-30-users-unique-repeated"]
                .candidate_config["staging"]
                .pre_test_fn,
                {
                    "name": "loadtest-test",
                    "staging_enabled": True,
                    "staging_autocommit": True,
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
