"""Git source resolution."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from pulp_juicer.sources import resolve_source


class TestResolveSource(unittest.TestCase):
    def test_remote_sources_are_shallow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "origin"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repository)], check=True)
            for content in ("first", "second"):
                (repository / "file.txt").write_text(content)
                subprocess.run(
                    ["git", "-C", str(repository), "add", "file.txt"], check=True
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "commit",
                        "-m",
                        content,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                if content == "first":
                    subprocess.run(
                        ["git", "-C", str(repository), "tag", "v1"], check=True
                    )

            for suffix, expected in (("#v1", "first"), ("", "second")):
                with self.subTest(ref=suffix):
                    checkout = resolve_source(
                        repository.as_uri() + suffix, "core", root, root / "results"
                    )
                    self.assertEqual((checkout / "file.txt").read_text(), expected)
                    self.assertEqual(
                        subprocess.check_output(
                            [
                                "git",
                                "-C",
                                str(checkout),
                                "rev-parse",
                                "--is-shallow-repository",
                            ],
                            text=True,
                        ).strip(),
                        "true",
                    )
