"""Unified command dispatch; browser dependencies load only when used."""

import argparse
import importlib
import sys
from collections.abc import Sequence

from . import __version__

COMMANDS = {
    "login": ("platforms.login", "Create an explicit temporary login state"),
    "stuonline": ("platforms.stuonline", "Discover or download shared-course documents"),
    "aicourse": ("platforms.aicourse", "Discover or download AI-course documents"),
    "chaoxing": ("platforms.chaoxing", "Discover or download Chaoxing (Xuexitong) documents"),
    "icourse163": (
        "platforms.icourse163",
        "Discover or download icourse163 (中国大学MOOC) documents and videos",
    ),
    "verify": ("verify", "Verify local downloaded files"),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="course-resource-download")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        subs.add_parser(name, help=help_text, add_help=False)
    args, forwarded = parser.parse_known_args(argv)
    module = importlib.import_module(f".{COMMANDS[args.command][0]}", __package__)
    try:
        return int(module.main(forwarded) or 0)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        from .common import safe_error

        print(f"Error: {safe_error(exc)}", file=sys.stderr)
        return 1
