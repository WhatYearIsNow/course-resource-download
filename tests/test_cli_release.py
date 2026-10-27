"""CLI/process and distribution contract tests, without real course access."""

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from course_resource_download import cli

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env():
    result = os.environ.copy()
    result["PYTHONPATH"] = str(ROOT / "src")
    result["PYTHONIOENCODING"] = "utf-8"
    return result


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["--version"],
        ["--help"],
        ["login", "--help"],
        ["stuonline", "--help"],
        ["aicourse", "--help"],
        ["chaoxing", "--help"],
        ["verify", "--help"],
    ],
)
def test_unified_cli_help_without_optional_browser_imports(command, tmp_path):
    code = """
import importlib.abc, sys
class DenyBrowser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'playwright', 'cv2', 'numpy'}:
            raise AssertionError('Optional browser dependency imported: ' + fullname)
sys.meta_path.insert(0, DenyBrowser())
from course_resource_download.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    run = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, *command],
        cwd=tmp_path,
        env=env(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == (2 if not command else 0), run.stderr


@pytest.mark.parametrize(
    "script", ["zh_login.py", "zh_stuonline.py", "zh_aicourse.py", "verify.py"]
)
def test_legacy_commands_from_unrelated_cwd(script, tmp_path):
    run = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / script), "--help"],
        cwd=tmp_path,
        env=env(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == 0, run.stderr
    assert "usage:" in run.stdout


def test_verify_missing_directory_returns_failure(tmp_path):
    run = subprocess.run(
        [sys.executable, "-m", "course_resource_download", "verify", str(tmp_path / "missing")],
        env=env(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode != 0


def test_package_allowlist_omits_secrets_and_build_outputs(tmp_path):
    package = load_script("package_skill")
    project = tmp_path / "project"
    project.mkdir()
    for name in [
        "SKILL.md",
        "LICENSE",
        "pyproject.toml",
        "credentials.json",
        "zh_state.json",
        "_manifest.json",
        ".env",
        "unreviewed.pdf",
    ]:
        (project / name).write_text("fixture", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "sample.py").write_text("pass\n", encoding="utf-8")
    archive = tmp_path / "release.skill"
    assert package.package(project, archive) == 4
    first = archive.read_bytes()
    package.package(project, archive)
    assert archive.read_bytes() == first
    with zipfile.ZipFile(archive) as content:
        assert sorted(Path(name).name for name in content.namelist()) == [
            "LICENSE",
            "SKILL.md",
            "pyproject.toml",
            "sample.py",
        ]
    assert load_script("release_check").inspect(archive) == []


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/absolute.py",
        "C:/private.py",
        "x/credentials.json",
        "x/_manifest.json",
        "x/.download.lock",
        "x/.env.local",
        "x/file.part",
    ],
)
def test_release_check_rejects_runtime_and_traversal(name):
    assert load_script("release_check").check_names([name])


def test_skill_metadata_references_and_license():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: course-resource-download" in text
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


def test_dispatch_forwards_options_and_returns_status(monkeypatch):
    class Module:
        @staticmethod
        def main(argv):
            assert argv == ["--out", "local"]
            return 1

    monkeypatch.setattr(cli.importlib, "import_module", lambda *args: Module)
    assert cli.main(["stuonline", "--out", "local"]) == 1


@pytest.mark.parametrize(
    "failure, expected",
    [
        (RuntimeError("failed https://example.test/file?token=private"), 1),
        (KeyboardInterrupt(), 130),
    ],
)
def test_dispatch_errors_have_nonzero_status_without_signed_url(
    monkeypatch, capsys, failure, expected
):
    class Module:
        @staticmethod
        def main(argv):
            raise failure

    monkeypatch.setattr(cli.importlib, "import_module", lambda *args: Module)
    assert cli.main(["aicourse"]) == expected
    captured = capsys.readouterr()
    assert "token=private" not in captured.err + captured.out
