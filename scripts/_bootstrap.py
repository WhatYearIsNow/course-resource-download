"""Run compatibility scripts from a source checkout, independent of cwd."""

import importlib
import sys
from pathlib import Path


def run(module: str) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    return int(importlib.import_module("course_resource_download." + module).main() or 0)
