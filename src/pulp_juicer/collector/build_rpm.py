"""Build the collector RPM with rpm-rs."""

import argparse
from pathlib import Path

from rpm_rs import FileOptions, PackageBuilder

PACKAGE_NAME = "pulp-juicer-collector"
VERSION = "0.8.0"
RELEASE = "1"
SOURCE_DIR = Path(__file__).resolve().parent


def build(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    package = PackageBuilder(
        PACKAGE_NAME, VERSION, "MIT", "noarch", "Pulp Juicer data collectors"
    )
    package.release(RELEASE)
    package.description("Runs periodic and on-demand collectors in the Pulp container.")
    package.with_file(
        str(SOURCE_DIR / "collector.py"),
        FileOptions.new("/usr/libexec/pulp-juicer/collector.py", permissions=0o644),
    )
    package.with_file(
        str(SOURCE_DIR / "postgres.py"),
        FileOptions.new("/usr/libexec/pulp-juicer/postgres.py", permissions=0o644),
    )
    package.with_file(
        str(SOURCE_DIR / "components.py"),
        FileOptions.new("/usr/libexec/pulp-juicer/components.py", permissions=0o644),
    )
    package.with_file(
        str(SOURCE_DIR / "postgres_snapshot.py"),
        FileOptions.new(
            "/usr/libexec/pulp-juicer/postgres_snapshot.py", permissions=0o644
        ),
    )
    for module in (
        "content_io",
        "content_io_snapshot",
        "content_temp",
        "task_db",
        "task_wait",
        "postgres_wait",
    ):
        package.with_file(
            str(SOURCE_DIR / f"{module}.py"),
            FileOptions.new(f"/usr/libexec/pulp-juicer/{module}.py", permissions=0o644),
        )
    package.with_file(
        str(SOURCE_DIR / "control.py"),
        FileOptions.new("/usr/libexec/pulp-juicer/control.py", permissions=0o644),
    )
    service_dir = "/etc/s6-overlay/s6-rc.d/pulp-juicer-collector"
    package.with_file(
        str(SOURCE_DIR / "s6/run"),
        FileOptions.new(f"{service_dir}/run", permissions=0o755),
    )
    package.with_file(
        str(SOURCE_DIR / "s6/type"),
        FileOptions.new(f"{service_dir}/type", permissions=0o644),
    )
    package.with_file_contents(
        b"",
        FileOptions.new(f"{service_dir}/dependencies.d/base", permissions=0o644),
    )
    package.with_file_contents(
        b"",
        FileOptions.new(
            "/etc/s6-overlay/s6-rc.d/user/contents.d/pulp-juicer-collector",
            permissions=0o644,
        ),
    )
    output = output_dir / f"{PACKAGE_NAME}-{VERSION}-{RELEASE}.noarch.rpm"
    package.build().write_to(str(output))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    print(build(parser.parse_args().output_dir))
