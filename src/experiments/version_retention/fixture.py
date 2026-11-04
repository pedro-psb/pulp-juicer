"""A manifest backed by 500,000 small, real files."""

import hashlib
import os
from pathlib import Path

from pulp_juicer import ServiceFixture, ServiceFixtureMetadata


class FileFixture(ServiceFixture):
    compose_file = Path(__file__).with_name("fixture-compose.yaml")

    def __init__(self, name, output_dir, *, count=500_000):
        super().__init__(name, output_dir)
        self.count = count

    def build(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.output_dir / "PULP_MANIFEST"
        if manifest.exists() and sum(1 for _ in manifest.open()) == self.count:
            return
        with manifest.open("w") as out:
            for index in range(self.count):
                shard = index // 10_000
                folder = self.output_dir / f"{shard:03d}"
                folder.mkdir(exist_ok=True)
                path = folder / f"{index:06d}.bin"
                payload = f"retention fixture shard {shard}\n".encode()
                source = folder / "source.bin"
                if not source.exists():
                    source.write_bytes(payload)
                if not path.exists():
                    os.link(source, path)
                relative = path.relative_to(self.output_dir)
                out.write(
                    f"{relative},{hashlib.sha256(payload).hexdigest()},{len(payload)}\n"
                )

    def generate_metadata(self):
        return ServiceFixtureMetadata(
            name=self.name,
            compose_file=self.compose_file,
            service_name="file_fixture",
            output_dir=self.output_dir,
            url="http://file_fixture:8000/",
            environment={"FIXTURE_DIR": str(self.output_dir)},
        )
