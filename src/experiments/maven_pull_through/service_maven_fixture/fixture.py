"""Build Maven artifacts and describe the fixture HTTP service."""

import argparse
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from pulp_juicer import ServiceFixture, ServiceFixtureMetadata


class MavenServiceFixture(ServiceFixture):
    compose_file = Path(__file__).with_name("compose.yaml")
    service_name = "maven_fixture"
    url = "http://maven_fixture:8000/"

    def __init__(self, name: str, output_dir: Path, *, count: int = 1000, size: int = 65536):
        super().__init__(name=name, output_dir=output_dir)
        if count < 1 or size < 1:
            raise ValueError("Fixture count and artifact size must be positive")
        self.count = count
        self.size = size

    def build(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for index in range(self.count):
            name = f"artifact-{index:06d}"
            path = f"org/example/loadtest/{name}/1.0/{name}-1.0.jar"
            payload = hashlib.shake_256(f"{self.name}:{index}".encode()).digest(self.size)
            jar = io.BytesIO()
            with ZipFile(jar, "w", compression=ZIP_STORED) as archive:
                member = ZipInfo("fixture.bin", date_time=(2024, 1, 1, 0, 0, 0))
                archive.writestr(member, payload)
            data = jar.getvalue()
            destination = self.output_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise ValueError(f"Fixture path already has different content: {destination}")
            else:
                destination.write_bytes(data)
            entries.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
        (self.output_dir / "manifest.json").write_text(json.dumps({"artifacts": entries}, indent=2) + "\n")

    def generate_metadata(self) -> ServiceFixtureMetadata:
        return ServiceFixtureMetadata(
            name=self.name,
            compose_file=self.compose_file,
            service_name=self.service_name,
            output_dir=self.output_dir,
            url=self.url,
            environment={"FIXTURE_DIR": str(self.output_dir)},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Maven service fixture.")
    parser.add_argument("--name", default="maven")
    parser.add_argument("--output-dir", type=Path, default=Path("_juice/fixture/maven"))
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--size", type=int, default=65536)
    options = parser.parse_args()

    fixture = MavenServiceFixture(
        name=options.name,
        output_dir=options.output_dir,
        count=options.count,
        size=options.size,
    )
    fixture.build()
    metadata = fixture.generate_metadata()
    print(json.dumps(asdict(metadata), default=str, indent=2))


if __name__ == "__main__":
    main()
