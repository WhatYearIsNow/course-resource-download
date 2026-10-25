"""Build a reproducible skill/source ZIP using an explicit source-file allowlist."""

import argparse
import zipfile
from pathlib import Path

ROOT_FILES = {
    "SKILL.md",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "pyproject.toml",
    "requirements.txt",
    "MANIFEST.in",
    ".gitignore",
    ".gitattributes",
    "REFACTOR_PLAN.md",
    "VALIDATION.md",
    ".editorconfig",
    "requirements-dev.txt",
    "maintenance.json",
    "MAINTENANCE.md",
}
TREES = {
    "src": {".py"},
    "scripts": {".py"},
    "tests": {".py"},
    "references": {".md"},
    "agents": {".yaml"},
    ".github": {".yml", ".yaml"},
}


def source_files(root: Path) -> list[Path]:
    files = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for directory, extensions in TREES.items():
        for path in (root / directory).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in extensions
                and "__pycache__" not in path.parts
            ):
                files.append(path)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source file escapes project directory")
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def package(root: Path, out: Path) -> int:
    root = root.resolve()
    required = ("SKILL.md", "LICENSE", "pyproject.toml")
    if not all((root / name).is_file() for name in required):
        raise ValueError("Missing skill, license or project metadata")
    out.parent.mkdir(parents=True, exist_ok=True)
    files = source_files(root)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = "course-resource-download/" + path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return len(files)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, default=Path("dist/course-resource-download.skill"))
    args = parser.parse_args(argv)
    count = package(args.root, args.out)
    print(f"Packaged {count} source files: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
