"""Offline folder and permission contracts for the stuonline adapter."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from course_resource_download.platforms import stuonline as stu


def file_row(**kwargs):
    return {
        "fid": None,
        "name": "Document",
        "ftype": "pdf",
        "candownload": "0",
        "onclick": "filePreviewIframe1('preview','Document','123','pdf',this)",
        **kwargs,
    }


def video_row(**kwargs):
    return {
        "fid": None,
        "name": "Clip",
        "ftype": "mp4",
        "opid": "14494358",
        "candownload": "0",
        "onclick": (
            "tm_data_show_proxy(this,14494358,'mp4','mp4',"
            "'http://video.zhihuishu.com/zhs/onlineschool/COURSE_FOLDER/201708/a.mp4','x')"
        ),
        **kwargs,
    }


class CountingPage:
    """Minimal page whose data-row count follows a scripted sequence."""

    def __init__(self, counts):
        self.counts = list(counts)
        self.waits = 0

    def wait_for_selector(self, *args, **kwargs):
        return None

    def evaluate(self, script):
        return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]

    def wait_for_timeout(self, milliseconds):
        self.waits += 1


class FolderContracts(unittest.TestCase):
    def args(self, **kwargs):
        return SimpleNamespace(
            include_video=kwargs.get("include_video", False),
            list_only=kwargs.get("list_only", False),
        )

    def test_denied_resource_never_calls_download_api(self):
        page = Mock()
        entry = {}
        stu.process_file(page, file_row(candownload="1"), "", "unused", self.args(), {}, entry)
        self.assertEqual(entry["status"], "permission_denied")
        page.evaluate.assert_not_called()

    def test_unknown_permission_never_calls_download_api(self):
        for flag in (None, "", "unknown", "2"):
            with self.subTest(flag=flag):
                page = Mock()
                entry = {}
                stu.process_file(
                    page, file_row(candownload=flag), "", "unused", self.args(), {}, entry
                )
                self.assertEqual(entry["status"], "failed")
                self.assertIn("permission flag", entry["error"])
                page.evaluate.assert_not_called()

    def test_stale_parent_table_does_not_confirm_folder_navigation(self):
        page = Mock(url="https://stuonline.zhihuishu.com/course")
        page.evaluate.side_effect = ["same-table", True]
        page.wait_for_function.side_effect = TimeoutError("Folder table unchanged")
        with patch.object(stu, "wait_for_table"), self.assertRaises(TimeoutError):
            stu.open_folder(page, page.url, ["child"])
        page.wait_for_function.assert_called_once()

    def test_ajax_filled_table_is_waited_for(self):
        page = CountingPage([1, 31, 31, 31])
        self.assertEqual(stu.wait_for_table(page), 31)
        self.assertEqual(page.waits, 3)

    def test_empty_folder_settles_at_zero(self):
        page = CountingPage([0, 0, 0])
        self.assertEqual(stu.wait_for_table(page), 0)

    def test_header_row_alone_is_not_a_material_table(self):
        # A page that never grows past the header must not be read as populated.
        page = CountingPage([0])
        self.assertEqual(stu.wait_for_table(page, timeout_ms=1), 0)

    def test_permission_zero_allows_metadata_request(self):
        page = Mock()
        page.evaluate.return_value = json.dumps({"dataUrl": "https://example.invalid/a.bin"})
        entry = {}
        stu.process_file(page, file_row(), "", "unused", self.args(), {}, entry)
        self.assertEqual(entry["status"], "skipped_non_document")
        page.evaluate.assert_called_once()

    def test_video_row_uses_the_media_url_not_a_download_id(self):
        page = Mock()
        entry = {}
        stu.process_file(
            page,
            video_row(),
            "",
            "unused",
            self.args(include_video=True, list_only=True),
            {},
            entry,
        )
        self.assertEqual(entry["status"], "discovered")
        self.assertEqual(
            entry["url"], "http://video.zhihuishu.com/zhs/onlineschool/COURSE_FOLDER/201708/a.mp4"
        )
        self.assertEqual(entry["file"], "Clip.mp4")
        page.evaluate.assert_not_called()

    def test_video_download_uses_the_media_url(self):
        page = Mock()
        entry = {}
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.object(stu, "download_atomic", return_value=10) as download,
            patch.object(stu, "is_usable", return_value=True),
        ):
            stu.process_file(
                page, video_row(), "", temporary, self.args(include_video=True), {}, entry
            )
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(
            download.call_args.args[0],
            "http://video.zhihuishu.com/zhs/onlineschool/COURSE_FOLDER/201708/a.mp4",
        )
        page.evaluate.assert_not_called()

    def test_video_without_a_media_url_is_failed(self):
        page = Mock()
        entry = {}
        stu.process_file(
            page,
            video_row(onclick="tm_data_show_proxy(this,1,'mp4','mp4')"),
            "",
            "unused",
            self.args(include_video=True),
            {},
            entry,
        )
        self.assertEqual(entry["status"], "failed")
        self.assertIn("video URL", entry["error"])
        page.evaluate.assert_not_called()

    def test_avi_video_is_skipped_as_unsupported(self):
        page = Mock()
        entry = {}
        stu.process_file(
            page,
            video_row(
                ftype="avi",
                name="Clip.avi",
                onclick=(
                    "tm_data_show_proxy(this,14494358,'mp4','avi',"
                    "'http://video.zhihuishu.com/zhs/onlineschool/COURSE_FOLDER/201708/a.avi','x')"
                ),
            ),
            "",
            "unused",
            self.args(include_video=True),
            {},
            entry,
        )
        self.assertEqual(entry["status"], "skipped_unsupported_format")
        page.evaluate.assert_not_called()

    def test_missing_url_is_failed(self):
        page = Mock()
        page.evaluate.return_value = "{}"
        entry = {}
        stu.process_file(page, file_row(), "", "unused", self.args(), {}, entry)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Missing", entry["error"])

    def test_missing_resource_identity_is_failed(self):
        page = Mock()
        entry = {}
        stu.process_file(page, file_row(onclick=None), "", "unused", self.args(), {}, entry)
        self.assertEqual(entry["status"], "failed")
        page.evaluate.assert_not_called()

    def test_interrupted_transfer_is_failed_in_checkpoint(self):
        page = Mock()
        page.evaluate.side_effect = [
            [file_row()],
            json.dumps({"dataUrl": "https://example.invalid/a.pdf"}),
        ]
        entries, snapshots = [], []
        with (
            tempfile.TemporaryDirectory(dir=".") as temporary,
            patch.object(stu, "open_folder", return_value=True),
            patch.object(stu, "download_atomic", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                stu.walk(
                    page,
                    "https://example.invalid",
                    [],
                    "",
                    temporary,
                    self.args(),
                    entries,
                    set(),
                    {},
                    [],
                    lambda: snapshots.append(entries[0]["status"]),
                )
        self.assertEqual(entries[0]["status"], "failed")
        self.assertTrue(snapshots)
        self.assertTrue(all(status == "failed" for status in snapshots))

    def test_default_video_skip(self):
        entry = {}
        page = Mock()
        stu.process_file(page, file_row(ftype="mp4"), "", "unused", self.args(), {}, entry)
        self.assertEqual(entry["status"], "skipped_video")
        page.evaluate.assert_not_called()

    def test_list_only_does_not_resolve_or_download(self):
        entry = {}
        page = Mock()
        stu.process_file(page, file_row(), "", "unused", self.args(list_only=True), {}, entry)
        self.assertEqual(entry["status"], "discovered")
        page.evaluate.assert_not_called()

    def test_failed_subfolder_is_recorded_and_sibling_is_processed(self):
        page = Mock()
        page.evaluate.side_effect = [
            [{"fid": "one", "name": "Broken"}, {"fid": "two", "name": "Allowed"}],
            [file_row(candownload="1")],
        ]
        entries, folders = [], []
        with patch.object(stu, "open_folder", side_effect=[True, False, True]):
            stu.walk(
                page,
                "https://example.invalid",
                [],
                "",
                "unused",
                self.args(),
                entries,
                set(),
                {},
                folders,
            )
        self.assertEqual([folder["status"] for folder in folders], ["ok", "failed", "ok"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "permission_denied")

    def test_folder_cycle_is_bounded_and_recorded(self):
        page = Mock()
        page.evaluate.return_value = [{"fid": "one", "name": "Loop"}]
        folders = []
        with patch.object(stu, "open_folder", return_value=True):
            stu.walk(
                page,
                "https://example.invalid",
                [],
                "",
                "unused",
                self.args(),
                [],
                set(),
                {},
                folders,
            )
        self.assertEqual(len(folders), 3)
        self.assertIn("already visited", folders[-1]["reason"])

    def test_folder_selection_excludes_siblings_and_ancestor_files(self):
        page = Mock()
        page.evaluate.side_effect = [
            [
                file_row(name="Root file"),
                {"fid": "one", "name": "Chapter"},
                {"fid": "two", "name": "Sibling"},
            ],
            [file_row(name="Selected file"), {"fid": "three", "name": "Descendant"}],
            [file_row(name="Descendant file")],
        ]
        args = self.args(list_only=True)
        args.folder = ["Chapter"]
        entries, folders = [], []
        with patch.object(stu, "open_folder", return_value=True) as open_folder:
            stu.walk(
                page, "https://example.invalid", [], "", "unused", args, entries, set(), {}, folders
            )
        self.assertEqual([entry["name"] for entry in entries], ["Selected file", "Descendant file"])
        self.assertEqual(
            [call.args[2] for call in open_folder.call_args_list], [[], ["one"], ["one", "three"]]
        )
        self.assertTrue(all(folder["status"] == "ok" for folder in folders))

    def test_nested_folder_selection_excludes_parent_files(self):
        page = Mock()
        page.evaluate.side_effect = [
            [{"fid": "one", "name": "Chapter"}],
            [file_row(name="Parent"), {"fid": "two", "name": "Section"}],
            [file_row(name="Wanted")],
        ]
        args = self.args(list_only=True)
        args.folder = ["Chapter/Section"]
        entries, folders = [], []
        with patch.object(stu, "open_folder", return_value=True):
            stu.walk(
                page, "https://example.invalid", [], "", "unused", args, entries, set(), {}, folders
            )
        self.assertEqual([entry["name"] for entry in entries], ["Wanted"])

    def test_missing_selected_folder_returns_failure(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "state.json")
            state.write_text("{}", encoding="utf-8")
            factory = MagicMock()
            page = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value
            page.evaluate.return_value = [
                file_row(name="Unselected"),
                {"fid": "one", "name": "Other"},
            ]
            with (
                patch.object(stu, "sync_playwright", factory),
                patch.object(stu, "open_folder", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = stu.main(
                    [
                        "--url",
                        "https://example.invalid",
                        "--out",
                        temporary,
                        "--state",
                        str(state),
                        "--folder",
                        "Missing",
                    ]
                )
            self.assertEqual(result, 1)
            report = json.loads(Path(temporary, "_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["resources"], [])
            self.assertEqual(report["folders"][-1]["reason"], "Requested folder not found")

    def test_folder_option_normalizes_and_rejects_unsafe_paths(self):
        args = stu.build_parser().parse_args(
            ["--url", "url", "--out", "out", "--folder", "Chapter\\Section/", "--folder", "Other"]
        )
        self.assertEqual(args.folder, ["Chapter/Section", "Other"])
        for value in ("", "..", "Chapter/../Other", "/Chapter", "C:/Chapter"):
            with self.subTest(value=value), self.assertRaises(Exception):
                stu.normalize_folder(value)

    def test_browser_failure_returns_nonzero_and_keeps_report(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "state.json")
            state.write_text("{}", encoding="utf-8")
            with (
                patch.object(stu, "sync_playwright", side_effect=RuntimeError("No browser")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = stu.main(
                    ["--url", "https://example.invalid", "--out", temporary, "--state", str(state)]
                )
            self.assertEqual(result, 1)
            report = json.loads(Path(temporary, "_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["folders"][0]["status"], "failed")

    def test_manifest_is_written_while_lock_is_held(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "state.json")
            state.write_text("{}", encoding="utf-8")
            original = stu.write_manifest
            observed = []

            def publish(*args, **kwargs):
                lock = stu.acquire_lock(temporary)
                observed.append(lock)
                if lock:
                    stu.release_lock(lock)
                return original(*args, **kwargs)

            with (
                patch.object(stu, "sync_playwright", side_effect=RuntimeError("No browser")),
                patch.object(stu, "write_manifest", side_effect=publish),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                stu.main(
                    ["--url", "https://example.invalid", "--out", temporary, "--state", str(state)]
                )
            self.assertEqual(observed, [None])


if __name__ == "__main__":
    unittest.main()
