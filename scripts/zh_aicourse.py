"""Compatibility command. Prefer `course-resource-download` after installation."""

from _bootstrap import run

if __name__ == "__main__":
    raise SystemExit(run("platforms.aicourse"))
