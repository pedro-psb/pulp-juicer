"""The long pull-through scenario requests unique artifacts before repeats."""

import json
import tempfile
import unittest
from pathlib import Path

from experiments.bucket_repository.experiment import EXPERIMENT
from experiments.bucket_repository.scenario_input import (
    LONG_STEP_REPEAT_ROUNDS,
    LONG_STEP_UNIQUE_COUNT,
    build_pull_through_unique_then_repeated,
)
from pulp_juicer.types import ServiceFixtureMetadata


class TestLongPullThrough(unittest.TestCase):
    def test_unique_phase_precedes_repeated_passes(self):
        scenario = next(
            item
            for item in EXPERIMENT.scenarios
            if item.id == "pull-through-step-30-users-unique-repeated"
        )
        self.assertEqual(scenario.run_order, (1, 2))
        self.assertEqual(scenario.requires[0].args, {"count": 15000, "size": 65536})
        self.assertEqual(scenario.load.users, 30)
        self.assertEqual(scenario.load.max_duration, "30m")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            artifacts = [
                {"path": f"artifact-{index:05d}.jar", "sha256": str(index)}
                for index in range(LONG_STEP_UNIQUE_COUNT)
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
            request_input = build_pull_through_unique_then_repeated(
                {"maven": fixture}, {"base_url": "http://pulp/"}, scenario.load
            )
        paths = [item["path"] for item in request_input["requests"]]
        unique_paths = [item["path"] for item in artifacts]
        self.assertEqual(LONG_STEP_REPEAT_ROUNDS, 1)
        self.assertEqual(len(paths), scenario.load.request_count)
        self.assertEqual(len(paths), 2 * LONG_STEP_UNIQUE_COUNT)
        self.assertEqual(paths[:LONG_STEP_UNIQUE_COUNT], unique_paths)
        self.assertEqual(paths[LONG_STEP_UNIQUE_COUNT:], unique_paths)


if __name__ == "__main__":
    unittest.main()
