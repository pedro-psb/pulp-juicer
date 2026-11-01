"""Bundle an execution's reports and source data into a portable archive."""

import os
import tempfile
import zipfile
from pathlib import Path


def archive_execution(execution_dir: Path) -> tuple[Path, bool]:
    """Return the sibling ZIP path and whether its contents were refreshed."""
    execution_dir = Path(execution_dir)
    archive = execution_dir.parent / f"{execution_dir.name}.zip"
    files = sorted(
        path
        for path in execution_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    names = [
        str(Path(execution_dir.name) / path.relative_to(execution_dir))
        for path in files
    ]
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as existing:
                members = {item.filename: item for item in existing.infolist()}
                if sorted(members) == names and all(
                    path.stat().st_size == members[name].file_size
                    and path.stat().st_mtime_ns <= archive.stat().st_mtime_ns
                    for path, name in zip(files, names)
                ):
                    return archive.resolve(), False
        except zipfile.BadZipFile:
            pass

    with tempfile.NamedTemporaryFile(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as output:
            for path, name in zip(files, names):
                output.write(path, arcname=name)
        os.replace(temporary_path, archive)
    finally:
        temporary_path.unlink(missing_ok=True)
    return archive.resolve(), True
