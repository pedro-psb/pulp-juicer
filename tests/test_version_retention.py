"""The retention comparison changes only the repository retention policy."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from experiments.version_retention.experiment import EXPERIMENT
from experiments.version_retention.scenario import change_one, seed_sql
from pulp_juicer.plan import build_test_plan
from pulp_juicer.types import PulpConnection


class TestVersionRetention(unittest.TestCase):
    def test_plan_has_two_candidates_and_only_sql_seed(self):
        compose = (
            Path(__file__).resolve().parents[1]
            / "src/experiments/version_retention/compose.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_test_plan(EXPERIMENT, compose, repository_root=Path(tmp))
        self.assertEqual(
            [run.candidate.name for run in plan.runs], ["retain-one", "unlimited"]
        )
        self.assertEqual({run.scenario_id for run in plan.runs}, {"sql-models"})
        self.assertTrue(
            all(not run.fixtures and not run.fixture_builds for run in plan.runs)
        )
        self.assertTrue(all(run.scenario_setup is seed_sql for run in plan.runs))
        self.assertEqual(plan.runs[0].candidate.sources, plan.runs[1].candidate.sources)

    def test_candidate_hooks_set_only_the_first_retention_limit(self):
        client = Mock()
        client.wait_for_created_resource.return_value = {"pulp_href": "/repository/"}
        with patch(
            "experiments.version_retention.scenario.PulpClient", return_value=client
        ):
            first = (
                EXPERIMENT.candidates[0]
                .pre_test_functions[0]
                .setup_fn(PulpConnection(), {}, "run001-retain-one")
            )
            first_payload = client.post.call_args.args[1]
            second = (
                EXPERIMENT.candidates[1]
                .pre_test_functions[0]
                .setup_fn(PulpConnection(), {}, "run002-unlimited")
            )
            second_payload = client.post.call_args.args[1]
        self.assertEqual(first_payload["retain_repo_versions"], 1)
        self.assertNotIn("retain_repo_versions", second_payload)
        self.assertEqual(first["retain_repo_versions"], 1)
        self.assertIsNone(second["retain_repo_versions"])

    def test_shared_seed_and_workload(self):
        runtime = SimpleNamespace(
            files=("compose.yaml",),
            environment={},
            command=Mock(
                return_value=SimpleNamespace(
                    returncode=0, stdout="content-id\n", stderr=""
                )
            ),
        )
        result = seed_sql(PulpConnection(), {}, "run001-retain-one", runtime)
        self.assertIn("/opt/seed_models.py", runtime.command.call_args.args)
        self.assertIn("retention-run001-retain-one", runtime.command.call_args.args)
        self.assertEqual(
            result["content_href"], "/pulp/api/v3/content/file/files/content-id/"
        )
        workload = change_one(
            {}, {"repository": "/repository/", **result}, EXPERIMENT.scenarios[0].load
        )
        self.assertEqual(len(workload["requests"]), 10)
        self.assertEqual(
            [next(iter(request["json"])) for request in workload["requests"]],
            ["remove_content_units", "add_content_units"] * 5,
        )


if __name__ == "__main__":
    unittest.main()
