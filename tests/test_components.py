"""Live Pulp component discovery and Pulp worker paths."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pulp_juicer.collector.components import DatabaseConnection, PulpComponents


class TestPulpComponents(unittest.TestCase):
    def test_discovers_workers_and_their_own_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = root / "service"
            services.mkdir()
            for name in (
                "pulpcore-api",
                "pulpcore-content",
                "pulpcore-worker@1",
                "postgresql",
            ):
                (services / name).mkdir()
            working = root / "tmp"
            task_dir = working / "301@pulp-host"
            (task_dir / "task").mkdir(parents=True)
            (task_dir / "task" / "artifact.tmp").write_text("data")
            pids = {
                "pulpcore-api": 100,
                "pulpcore-content": 200,
                "pulpcore-worker@1": 301,
                "pulpcore-worker@2": 302,
                "postgresql": 400,
            }

            def status(command, **kwargs):
                self.assertEqual(command[:3], ["s6-svstat", "-o", "pid"])
                return SimpleNamespace(stdout=f"{pids[Path(command[3]).name]}\n")

            def process(pid):
                children = {100: [101, 102], 200: [201]}
                return SimpleNamespace(
                    children=lambda: [
                        SimpleNamespace(pid=child) for child in children.get(pid, [])
                    ]
                )

            components = PulpComponents(
                services,
                working_directory=working,
                worker_name_template="{pid}@{hostname}",
                database=DatabaseConnection("pulp", "pulp"),
            )
            with (
                patch(
                    "pulp_juicer.collector.components.subprocess.run",
                    side_effect=status,
                ),
                patch(
                    "pulp_juicer.collector.components.psutil.Process",
                    side_effect=process,
                ),
                patch(
                    "pulp_juicer.collector.components.socket.gethostname",
                    return_value="pulp-host",
                ),
            ):
                self.assertEqual(
                    [worker.pid for worker in components.api_workers], [101, 102]
                )
                self.assertEqual(
                    [worker.pid for worker in components.content_workers], [201]
                )
                self.assertEqual(
                    [worker.pid for worker in components.task_workers], [301]
                )
                self.assertEqual(
                    components.task_workers[0].temp_files(),
                    (task_dir / "task" / "artifact.tmp",),
                )
                self.assertEqual(components.postgres.pid, 400)
                self.assertEqual(
                    components.postgres.connection.uri, "postgresql://pulp@/pulp"
                )
                (services / "pulpcore-worker@2").mkdir()
                self.assertEqual(
                    [worker.pid for worker in components.task_workers], [301, 302]
                )

    def test_uses_effective_pulp_settings_for_paths_and_connection(self):
        settings = SimpleNamespace(
            WORKING_DIRECTORY="/custom/tmp",
            WORKER_NAME_TEMPLATE="{hostname}-{pid}",
            DATABASES={
                "default": {
                    "NAME": "pulp-db",
                    "USER": "pulp-user",
                    "PASSWORD": "secret",
                    "HOST": "/var/run/postgresql",
                    "PORT": "5432",
                }
            },
        )
        components = PulpComponents()
        with patch.object(components, "_settings", return_value=settings):
            self.assertEqual(components.working_directory, Path("/custom/tmp"))
            self.assertEqual(components.worker_name_template, "{hostname}-{pid}")
            self.assertEqual(
                components.database.uri,
                "postgresql://pulp-user:secret@/pulp-db?host=%2Fvar%2Frun%2Fpostgresql&port=5432",
            )
