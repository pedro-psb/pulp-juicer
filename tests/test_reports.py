"""The report discovers completed experiment runs."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pulp_juicer.cli import cli
from pulp_juicer.report import completed_runs


class TestReportDiscovery(unittest.TestCase):
    def test_discovers_experiment_result_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "burst" / "normal" / "run002"
            hidden = root / "_sources" / "normal" / "run003"
            for directory in (current, hidden):
                directory.mkdir(parents=True)
                (directory / "locust_stats.csv").write_text("Name\nAggregated\n")
            self.assertEqual(set(completed_runs(root)), {current})

    def test_report_command_handles_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            response = CliRunner().invoke(cli, ["report", "--results", str(results)])
        self.assertEqual(response.exit_code, 0, response.output)
        self.assertIn("No completed runs found", response.output)

    def test_report_command_renders_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            run_dir = results / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            (run_dir / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                "Aggregated,2,0,1.5,12,20\n"
            )

            def fake_quarto(command, **kwargs):
                self.assertEqual(kwargs["cwd"], results)
                (results / "report.html").write_text("<html>report</html>")

            with patch("pulp_juicer.report.shutil.which", return_value="/bin/true"), \
                 patch("pulp_juicer.report.subprocess.run", side_effect=fake_quarto) as render:
                response = CliRunner().invoke(cli, ["report", "--results", str(results)])

            self.assertEqual(response.exit_code, 0, response.output)
            self.assertIn(f"Report: {results / 'report.html'}", response.output)
            self.assertTrue((results / "report.html").is_file())
            document = (results / "report.qmd").read_text()
            self.assertIn("Pulp Juicer load test", document)
            self.assertIn("| burst | run001 | normal | 2 | 0 |", document)
            render.assert_called_once()


if __name__ == "__main__":
    unittest.main()
