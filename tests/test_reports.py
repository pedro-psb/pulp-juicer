"""Each execution has its own report, rendered only once."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pulp_juicer.cli import cli
from pulp_juicer.report import completed_runs
from pulp_juicer.trial_report import render_trial_report


class TestReportDiscovery(unittest.TestCase):
    def test_discovers_runs_within_one_execution(self):
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

    def test_report_command_renders_each_missing_execution_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            first = results / "maven-pull-through__burst__run001__001"
            second = results / "maven-pull-through__burst__run001__002"
            existing = results / "maven-pull-through__burst__run001__003"
            for execution in (first, second):
                run_dir = execution / "burst" / "normal" / "run001"
                run_dir.mkdir(parents=True)
                (run_dir / "locust_stats.csv").write_text(
                    "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                    "Aggregated,2,1,1.5,12,20\n"
                )
            existing.mkdir()
            (existing / "report.html").write_text("<html>existing</html>")
            (existing / "report.qmd").write_text("existing source")
            existing_run = existing / "burst" / "normal" / "run001"
            existing_run.mkdir(parents=True)
            (existing_run / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                "Aggregated,2,0,1.5,12,20\n"
            )

            def fake_quarto(command, **kwargs):
                self.assertIn(kwargs["cwd"], (first, second, existing))
                output = command[command.index("--output") + 1]
                (kwargs["cwd"] / output).write_text("<html>new</html>")

            with (
                patch("pulp_juicer.report.shutil.which", return_value="/bin/true"),
                patch(
                    "pulp_juicer.report.subprocess.run", side_effect=fake_quarto
                ) as render,
            ):
                response = CliRunner().invoke(
                    cli, ["report", "--results", str(results)]
                )

            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(
                response.output.splitlines(),
                [
                    "New Reports:",
                    str(first / "report.html"),
                    str(first / "trials.html"),
                    str(second / "report.html"),
                    str(second / "trials.html"),
                    str(existing / "trials.html"),
                    "",
                    "Existing:",
                    str(existing / "report.html"),
                ],
            )
            for execution in (first, second):
                document = (execution / "report.qmd").read_text()
                self.assertIn(execution.name, document)
                self.assertIn(
                    f"| {execution.name} | burst | run001 | normal | 2 | 1 |",
                    document,
                )
            self.assertEqual((existing / "report.qmd").read_text(), "existing source")
            self.assertEqual(
                (existing / "report.html").read_text(), "<html>existing</html>"
            )
            self.assertEqual(render.call_count, 5)

    def test_trial_report_compares_run_level_results_and_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp) / "maven-pull-through__burst__001"
            execution.mkdir()
            cases = (
                ("normal", "run001", 10, 100, 200, 50),
                ("staging", "run002", 15, 80, 150, 40),
                ("normal", "run003", 12, 120, 240, 60),
                ("staging", "run004", 17, 90, 170, 45),
            )
            for candidate, run_id, rate, mean, p95, cpu in cases:
                run_dir = execution / "burst" / candidate / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "locust_stats.csv").write_text(
                    "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                    f"Aggregated,2,0,{rate},{mean},{p95}\n"
                )
                (run_dir / "run_metadata.json").write_text(
                    '{"load":{"users":2,"request_count":2},'
                    '"fixture":{"manifest_sha256":"same"},'
                    '"result":{"expected_requests":2,"completed_requests":2,'
                    '"load_start_utc":"2026-01-01T00:00:00+00:00"}}'
                )
                (run_dir / "requests.csv").write_text(
                    "started_at_utc,completed_at_utc\n"
                    "2026-01-01T00:00:00+00:00,2026-01-01T00:00:01+00:00\n"
                    "2026-01-01T00:00:01+00:00,2026-01-01T00:00:02+00:00\n"
                )
                (run_dir / "db_cpu.csv").write_text(
                    f"timestamp_utc,postgres_cpu_pct\n2026-01-01T00:00:01+00:00,{cpu}\n"
                )
                (run_dir / "db_cpu_summary.csv").write_text(
                    f"postgres_cpu_mean_pct\n{cpu}\n"
                )

            def fake_quarto(command, **kwargs):
                (kwargs["cwd"] / "trials.html").write_text("<html>trials</html>")

            with (
                patch(
                    "pulp_juicer.trial_report.shutil.which", return_value="/bin/true"
                ),
                patch(
                    "pulp_juicer.trial_report.subprocess.run", side_effect=fake_quarto
                ) as quarto,
            ):
                report = render_trial_report(execution)

            self.assertEqual(report, execution / "trials.html")
            self.assertEqual(quarto.call_count, 1)
            source = (execution / "trials.qmd").read_text()
            self.assertIn("| normal | 2 | 11.00 (10.00–12.00) |", source)
            self.assertIn("| staging | 2 | 16.00 (15.00–17.00) |", source)
            self.assertIn("| staging | 5.00 | -25.00 | -60.00 |", source)
            self.assertLess(
                source.index("| staging | run002 |"),
                source.index("| normal | run003 |"),
            )
            for metric in ("completed", "throughput", "starts", "cpu"):
                svg = execution / f"trials_burst_{metric}.svg"
                self.assertTrue(svg.is_file())
                ET.parse(svg)
                self.assertIn("normal / run001", svg.read_text())
                self.assertIn("staging / run004", svg.read_text())
            self.assertIn("Time since load start (s)", svg.read_text())
            with patch("pulp_juicer.trial_report.shutil.which") as which:
                self.assertEqual(render_trial_report(execution), report)
            which.assert_not_called()

    def test_existing_report_needs_no_quarto(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            execution = results / "maven-pull-through__001"
            execution.mkdir(parents=True)
            (execution / "report.html").write_text("existing")
            (execution / "trials.html").write_text("existing trials")
            with patch("pulp_juicer.report.shutil.which") as which:
                response = CliRunner().invoke(
                    cli, ["report", "--results", str(results)]
                )
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(
                response.output.splitlines(),
                [
                    "Existing:",
                    str(execution / "report.html"),
                    str(execution / "trials.html"),
                ],
            )
            which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
