"""Check release archives for unsafe paths and known runtime/credential artifacts.

This checks packaging hygiene, not the absence of every possible secret in source.
"""

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def check_names(names: list[str]) -> list[str]:
    errors = []
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            errors.append("unsafe path: " + name)
        for part in path.parts:
            lowered = part.lower()
            if (
                lowered
                in {
                    "credentials.json",
                    "zh_state.json",
                    "state.json",
                    "_manifest.json",
                    "_discovery.json",
                    ".env",
                    ".git",
                    ".venv",
                    "__pycache__",
                    ".course-resource-download",
                }
                or lowered.startswith((".download.lock", "_cap_", "_panel.", ".env."))
                or lowered.endswith((".part", ".pyc"))
            ):
                errors.append("runtime artifact: " + name)
                break
    return errors


def inspect(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            errors = check_names(archive.namelist())
            bad = archive.testzip()
            if bad:
                errors.append("invalid CRC: " + bad)
            for entry in archive.infolist():
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    errors.append("symlink: " + entry.filename)
            return errors
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            return check_names([m.name for m in members]) + [
                "unsupported member: " + m.name for m in members if not (m.isfile() or m.isdir())
            ]
    return ["unsupported archive: " + path.name]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args(argv)
    files = sorted(
        p
        for p in args.directory.iterdir()
        if p.is_file() and (p.suffix in {".whl", ".skill", ".zip"} or p.name.endswith(".tar.gz"))
    )
    if not files:
        print("No release archives found")
        return 1
    errors = []
    for path in files:
        problems = inspect(path)
        errors.extend(f"{path.name}: {e}" for e in problems)
        print(f"{path.name}: {'FAIL' if problems else 'OK'}")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
