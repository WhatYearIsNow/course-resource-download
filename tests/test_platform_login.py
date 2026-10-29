"""Credential handling and interactive-login tests, using a fake browser only."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from course_resource_download.platforms import login


class LoginContracts(unittest.TestCase):
    def args(self, **kwargs):
        return SimpleNamespace(
            phone=kwargs.get("phone"), pwd=kwargs.get("pwd"), credentials=kwargs.get("credentials")
        )

    def test_environment_credentials_take_priority(self):
        with patch.dict(
            os.environ,
            {"ZHS_PHONE": "environment-phone", "ZHS_PWD": "environment-password"},
            clear=True,
        ):
            self.assertEqual(
                login.load_credentials(self.args(phone="cli-phone", pwd="cli-password")),
                ("environment-phone", "environment-password"),
            )

    def test_explicit_file_fills_only_missing_values(self):
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            credentials = Path(temporary, "credentials.json")
            credentials.write_text(
                json.dumps({"phone": "file-phone", "password": "file-password"}), encoding="utf-8"
            )
            self.assertEqual(
                login.load_credentials(self.args(phone="cli-phone", credentials=str(credentials))),
                ("cli-phone", "file-password"),
            )
            # The stream was closed before return (also permits deletion on Windows).
            credentials.unlink()

    def test_no_implicit_credentials_file_read(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("builtins.open", side_effect=AssertionError("Unexpected file access")),
        ):
            self.assertEqual(login.load_credentials(self.args()), (None, None))

    def test_malformed_credentials_fail_without_echoing_contents(self):
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            credentials = Path(temporary, "credentials.json")
            credentials.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "object"):
                login.load_credentials(self.args(credentials=str(credentials)))

    def test_default_service_contains_no_private_identifiers(self):
        self.assertEqual(login.DEFAULT_SERVICE, "https://onlineweb.zhihuishu.com/onlinestuh5")
        args = login.build_parser().parse_args([])
        self.assertFalse(args.auto_captcha)
        self.assertEqual(args.state, os.path.join(".course-resource-download", "state.json"))

    def test_redirect_to_unrelated_site_is_not_login_success(self):
        page = Mock(url="https://example.invalid/")
        self.assertFalse(login.is_logged_in(page, login.DEFAULT_SERVICE))
        page.url = login.DEFAULT_SERVICE
        self.assertTrue(login.is_logged_in(page, login.DEFAULT_SERVICE))

    def test_manual_login_does_not_require_credentials_or_solver(self):
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            state = Path(temporary, "output.json")
            factory = MagicMock()
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.url = login.DEFAULT_SERVICE
            page.get_by_role.return_value.count.return_value = 0

            def write_state(path):
                Path(path).write_text('{"cookies": []}', encoding="utf-8")

            context.storage_state.side_effect = write_state
            with (
                patch.object(login, "sync_playwright", factory),
                patch.object(login, "solve_challenge") as solve,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = login.main(["--state", str(state)])
            self.assertEqual(result, 0)
            self.assertTrue(state.is_file())
            self.assertEqual(list(Path(temporary).glob(".state-*")), [])
            solve.assert_not_called()
            page.get_by_placeholder.assert_not_called()
            browser.close.assert_called_once()

    def test_existing_state_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "output.json")
            state.write_text("original", encoding="utf-8")
            with (
                patch.object(login, "sync_playwright") as browser,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = login.main(["--state", str(state)])
            self.assertEqual(result, 1)
            self.assertEqual(state.read_text(encoding="utf-8"), "original")
            browser.assert_not_called()

    def test_failed_state_save_preserves_existing_and_cleans_staging(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "output.json")
            state.write_text("original", encoding="utf-8")
            context = Mock()

            def fail(path):
                Path(path).write_text("partial", encoding="utf-8")
                raise OSError("Disk error")

            context.storage_state.side_effect = fail
            with self.assertRaises(OSError):
                login.save_state(context, state)
            self.assertEqual(state.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(temporary).glob(".state-*")), [])

    def test_state_created_during_login_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "output.json")
            context = Mock()

            def complete_login(path):
                Path(path).write_text("new-state", encoding="utf-8")
                state.write_text("other-process-state", encoding="utf-8")

            context.storage_state.side_effect = complete_login
            with self.assertRaises(FileExistsError):
                login.save_state(context, state)
            self.assertEqual(state.read_text(encoding="utf-8"), "other-process-state")
            self.assertEqual(list(Path(temporary).glob(".state-*")), [])

    def test_explicit_overwrite_replaces_complete_state(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "output.json")
            state.write_text("old-state", encoding="utf-8")
            context = Mock()
            context.storage_state.side_effect = lambda path: Path(path).write_text(
                "new-state", encoding="utf-8"
            )
            login.save_state(context, state, overwrite=True)
            self.assertEqual(state.read_text(encoding="utf-8"), "new-state")

    def test_login_error_redacts_fill_values_and_signed_url(self):
        error = ValueError(
            "fill('secret-password') phone private-phone https://example.invalid/?token=secret"
        )
        result = login.login_error(error, "private-phone", "secret-password")
        self.assertNotIn("secret", result)
        self.assertNotIn("private-phone", result)

    def test_invalid_timeouts_rejected_before_browser(self):
        for timeout in ("0", "-1", "nan", "inf"):
            with (
                self.subTest(timeout=timeout),
                patch.object(login, "sync_playwright") as browser,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    login.main(["--timeout", timeout])
                browser.assert_not_called()

    def test_invalid_service_is_parser_error(self):
        for service in (
            "http://onlineweb.zhihuishu.com",
            "https://example.invalid",
            "https://[bad",
        ):
            with self.subTest(service=service), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    login.main(["--service", service])

    def test_interrupted_login_closes_browser(self):
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            state = Path(temporary, "output.json")
            factory = MagicMock()
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            browser.new_context.return_value.new_page.side_effect = KeyboardInterrupt
            with (
                patch.object(login, "sync_playwright", factory),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = login.main(["--state", str(state)])
            self.assertEqual(result, 1)
            self.assertFalse(state.exists())
            browser.close.assert_called_once()

    def test_service_kind_covers_both_platforms(self):
        for hostname, expected in (
            ("onlineweb.zhihuishu.com", "zhihuishu"),
            ("zhihuishu.com", "zhihuishu"),
            ("passport2.chaoxing.com", "chaoxing"),
            ("chaoxing.com", "chaoxing"),
            ("example.com", None),
            (None, None),
        ):
            with self.subTest(hostname=hostname):
                self.assertEqual(login.service_kind(hostname), expected)

    def test_chaoxing_credentials_are_rejected_without_browser(self):
        for extra in (["--phone", "x"], ["--pwd", "x"], ["--auto-captcha"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    login.main(["--service", "https://passport2.chaoxing.com/login", *extra])


if __name__ == "__main__":
    unittest.main()
