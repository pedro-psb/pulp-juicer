"""The upload scenario reuses fixture JARs and targets Maven content creation."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from experiments.bucket_repository.experiment import EXPERIMENT
from experiments.bucket_repository.scenario_input import (
    build_pull_through_repeated,
    build_upload,
)
from pulp_juicer import sample_sequence
from pulp_juicer.plan import build_test_plan
from pulp_juicer.types import ServiceFixtureMetadata, UnitaryImpulseLoad


class TestUploadScenario(unittest.TestCase):
    def test_scenario_selected_stats_accepts_a_list(self):
        scenario = EXPERIMENT.scenarios[0]
        self.assertIn("request_completions", scenario.selected_stats)
        copied = replace(scenario, selected_stats=["task_dispatches", "trial_phases"])
        self.assertEqual(copied.selected_stats, ("task_dispatches", "trial_phases"))

    def test_repeated_pull_through_uses_the_same_100_resources_ten_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            artifacts = [
                {"path": f"artifact-{index:03d}.jar", "sha256": f"hash-{index}"}
                for index in range(100)
            ]
            (directory / "manifest.json").write_text(
                json.dumps({"artifacts": artifacts})
            )
            fixture = ServiceFixtureMetadata(
                "maven",
                directory / "compose.yaml",
                "fixture",
                directory,
                "http://fixture",
            )
            requests = build_pull_through_repeated(
                {"maven": fixture},
                {"base_url": "http://pulp/"},
                UnitaryImpulseLoad(request_count=1000),
            )["requests"]
            self.assertEqual(len(requests), 1000)
            for block in range(10):
                self.assertEqual(
                    [
                        item["path"]
                        for item in requests[block * 100 : (block + 1) * 100]
                    ],
                    [item["path"] for item in artifacts],
                )

    def test_upload_and_pull_through_share_fixture_directory(self):
        compose = (
            Path(__file__).resolve().parents[1]
            / "src/experiments/bucket_repository/compose.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=root,
                scenario_id="upload-impulse",
            )
            pull = build_test_plan(
                EXPERIMENT,
                compose,
                repository_root=root,
                scenario_id="pull-through-1k-impulse",
            )
            self.assertEqual(
                upload.runs[0].fixtures["maven"].output_dir,
                pull.runs[0].fixtures["maven"].output_dir,
            )

    def test_impulse_reuses_generated_artifacts(self):
        scenario = next(
            scenario
            for scenario in EXPERIMENT.scenarios
            if scenario.id == "upload-impulse"
        )
        self.assertEqual(scenario.driver, "http_upload")
        self.assertEqual(scenario.load.request_count, 100)
        self.assertEqual(
            scenario.run_order,
            sample_sequence(len(scenario.run_order) // 2, (1, 2)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            paths = [
                f"org/example/loadtest/artifact-{index:06d}/1.0/artifact-{index:06d}-1.0.jar"
                for index in range(2)
            ]
            for path in paths:
                target = directory / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"jar")
            (directory / "manifest.json").write_text(
                json.dumps({"artifacts": [{"path": path} for path in paths]})
            )
            fixture = ServiceFixtureMetadata(
                "maven",
                directory / "compose.yaml",
                "fixture",
                directory,
                "http://fixture",
            )
            result = build_upload(
                {"maven": fixture},
                {
                    "api_base_url": "http://127.0.0.1:5001/",
                    "repository": "/pulp/api/v3/repositories/maven/maven/123/",
                    "staging_enabled": False,
                },
                UnitaryImpulseLoad(request_count=2),
            )
            staging_result = build_upload(
                {"maven": fixture},
                {
                    "api_base_url": "http://127.0.0.1:5001/",
                    "repository": "/pulp/api/v3/repositories/maven/maven/123/",
                    "staging_enabled": True,
                },
                UnitaryImpulseLoad(request_count=2),
            )
        self.assertEqual(result["base_url"], "http://127.0.0.1:5001")
        self.assertEqual(len(result["requests"]), 2)
        self.assertEqual(
            [request["relative_path"] for request in result["requests"]], paths
        )
        self.assertTrue(
            all(request["expected_status"] == 201 for request in result["requests"])
        )
        self.assertTrue(
            all(
                request["path"] == "pulp/api/v3/content/maven/artifact/upload/"
                for request in result["requests"]
            )
        )
        self.assertTrue(all("repository" not in r for r in result["requests"]))
        self.assertEqual(
            result["modify_repository"],
            "/pulp/api/v3/repositories/maven/maven/123/",
        )
        self.assertIsNone(staging_result["modify_repository"])
        self.assertEqual(
            {request["repository"] for request in staging_result["requests"]},
            {"/pulp/api/v3/repositories/maven/maven/123/"},
        )


if __name__ == "__main__":
    unittest.main()
