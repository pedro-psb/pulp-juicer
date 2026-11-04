"""Maven candidate setup and Pulp settings for different implementations."""

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from experiments.bucket_repository.experiment import EXPERIMENT
from pulp_juicer.execution import _compose_inputs
from pulp_juicer.plan import build_test_plan
from pulp_juicer.types import PulpConnection, ServiceFixtureMetadata


class TestMavenCandidateSetup(unittest.TestCase):
    def test_pulp_settings_apply_only_to_selected_candidate(self):
        compose = (
            Path(__file__).resolve().parents[1]
            / "src/experiments/bucket_repository/compose.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=Path(tmp),
                scenario_id="burst",
            )
            normal = next(run for run in plan.runs if run.candidate.name == "normal")
            staging = next(run for run in plan.runs if run.candidate.name == "staging")
            normal_files, _ = _compose_inputs(plan, normal)
            staging_files, _ = _compose_inputs(plan, staging)
            self.assertEqual(len(staging_files), len(normal_files) + 1)
            override = json.loads(staging_files[-1].read_text())
            encoded = override["services"]["pulp"]["environment"][
                "JUICER_PULP_SETTINGS_JSON"
            ]
            self.assertEqual(json.loads(encoded), dict(staging.candidate.pulp_settings))

        settings_path = compose.parent / "settings/settings.py"
        with patch.dict(os.environ, {"PULP_SECRET_KEY": "test"}, clear=True):
            normal_settings = runpy.run_path(str(settings_path))
        self.assertNotIn("STAGING_AUTOCOMMIT_DELAY_SECONDS", normal_settings)
        with patch.dict(
            os.environ,
            {"PULP_SECRET_KEY": "test", "JUICER_PULP_SETTINGS_JSON": encoded},
            clear=True,
        ):
            staging_settings = runpy.run_path(str(settings_path))
        self.assertEqual(
            staging_settings["STAGING_AUTOCOMMIT_DELAY_SECONDS"],
            staging.candidate.pulp_settings["STAGING_AUTOCOMMIT_DELAY_SECONDS"],
        )
        self.assertEqual(
            staging_settings["STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS"],
            staging.candidate.pulp_settings["STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS"],
        )

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
