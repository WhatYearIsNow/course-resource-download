"""Offline platform contracts: no live service, credential or browser access."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from course_resource_download.platforms import aicourse as ai


def detail(document="document", activity="activity", resources=None):
    return json.dumps(
        {
            "data": {
                "documentId": document,
                "taskId": activity,
                "activityName": "Duplicate name",
                "detailData": {
                    "knowledgeDetailList": [
                        {"nodeName": "Knowledge", "resourcesList": resources or []}
                    ]
                },
            }
        }
    )


class FakePage:
    def __init__(self, capture, pages):
        self.capture = capture
        self.pages = iter(pages)
        self.url = ai.BASE
        self.clicks = 0

    def goto(self, *args, **kwargs):
        self.capture.details.extend(next(self.pages, []))

    def wait_for_timeout(self, milliseconds):
        pass

    def evaluate(self, script):
        return False

    def click(self, *args, **kwargs):
        self.clicks += 1
        self.capture.details.extend(next(self.pages, []))


class ActivityContracts(unittest.TestCase):
    def args(self, **kwargs):
        return SimpleNamespace(course_id="course", class_id="class", max_activities=10, **kwargs)

    def test_first_response_must_match_document(self):
        capture = ai.ResponseCapture()
        page = FakePage(capture, [[detail("old", "first")]])
        store, records = [], []
        ai.traverse_tasks(
            page,
            capture,
            [{"documentId": "current", "totalActivities": 1}],
            self.args(),
            "map",
            store,
            records,
        )
        self.assertEqual(records[0]["status"], "failed")
        self.assertEqual(records[0]["visitedActivities"], 0)
        self.assertEqual(store, [])

    def test_late_other_task_response_ignored(self):
        capture = ai.ResponseCapture()
        page = FakePage(
            capture, [[detail(activity="one")], [detail("old", "two"), detail(activity="two")]]
        )
        records = []
        ai.traverse_tasks(
            page,
            capture,
            [{"documentId": "document", "totalActivities": 2}],
            self.args(),
            "map",
            [],
            records,
        )
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["visitedActivities"], 2)

    def test_cycle_counts_distinct_activities(self):
        capture = ai.ResponseCapture()
        page = FakePage(
            capture, [[detail(activity="one")], [detail(activity="two")], [detail(activity="one")]]
        )
        records = []
        ai.traverse_tasks(
            page,
            capture,
            [{"documentId": "document", "totalActivities": 3}],
            self.args(),
            "map",
            [],
            records,
        )
        self.assertEqual(records[0]["status"], "incomplete")
        self.assertEqual(records[0]["visitedActivities"], 2)
        self.assertEqual(page.clicks, 2)

    def test_missing_task_identity_is_failure(self):
        capture = ai.ResponseCapture()
        records = []
        ai.traverse_tasks(
            FakePage(capture, []),
            capture,
            [{"totalActivities": 0}],
            self.args(),
            "map",
            [],
            records,
        )
        self.assertEqual(records[0]["status"], "failed")

    def test_map_uid_response_can_arrive_after_cards(self):
        capture = ai.ResponseCapture()
        capture.cards = '{"data": {"teachTasks": []}}'
        page = Mock()
        page.wait_for_timeout.side_effect = lambda duration: setattr(
            capture, "mapuid", "delayed-map"
        )
        result = ai.course_map_uid(page, capture, [{"totalActivities": 1}])
        self.assertEqual(result, "delayed-map")
        page.wait_for_timeout.assert_called_once()

    def test_locked_tasks_need_no_map_uid(self):
        capture = ai.ResponseCapture()
        page = Mock()
        tasks = [
            {"totalActivities": 2, "canClick": False},
            {"totalActivities": 4, "lockReason": 1},
            {"totalActivities": 0},
        ]
        self.assertIsNone(ai.course_map_uid(page, capture, tasks))
        page.wait_for_timeout.assert_not_called()

    def test_locked_and_zero_activity_tasks_do_not_paginate(self):
        capture = ai.ResponseCapture()
        page = FakePage(capture, [])
        records = []
        ai.traverse_tasks(
            page,
            capture,
            [
                {"documentId": "one", "totalActivities": 3, "canClick": False},
                {"documentId": "two", "totalActivities": 0},
            ],
            self.args(),
            "map",
            [],
            records,
        )
        self.assertEqual([record["status"] for record in records], ["locked", "no_activities"])
        self.assertEqual(page.clicks, 0)

    def test_activity_limit_is_incomplete(self):
        capture = ai.ResponseCapture()
        page = FakePage(capture, [[detail(activity="one")]])
        records = []
        args = self.args()
        args.max_activities = 1
        ai.traverse_tasks(
            page,
            capture,
            [{"documentId": "document", "totalActivities": 4}],
            args,
            "map",
            [],
            records,
        )
        self.assertEqual(records[0]["status"], "incomplete")
        self.assertEqual(page.clicks, 0)

    def test_no_url_resources_are_never_dropped(self):
        store = []
        ai.harvest_detail(
            detail(
                resources=[
                    {"resourcesDataType": 21, "resourcesName": "Book"},
                    {"resourcesDataType": 11, "resourcesName": "Document"},
                ]
            ),
            store,
        )
        ai.harvest_cards(
            json.dumps({"data": {"teachTasks": [{"resources": [{"fileName": "Attachment"}]}]}}),
            store,
        )
        self.assertEqual(len(store), 3)
        self.assertEqual(len(ai.deduplicate_resources(store)), 3)
        self.assertEqual(
            [ai.resource_entry(resource)["status"] for resource in store],
            ["skipped_ebook", "failed", "failed"],
        )

    def test_whitelist_uses_actual_extension_even_for_video_label(self):
        for include_video in (False, True):
            with self.subTest(include_video=include_video):
                entry = ai.resource_entry(
                    {"url": "https://example.invalid/file.bin", "type": 22}, include_video
                )
                self.assertEqual(entry["status"], "skipped_non_document")
        entry = ai.resource_entry({"url": "https://example.invalid/file.pdf", "type": 22})
        self.assertEqual(entry["status"], "discovered")

    def test_malformed_urls_are_reported_as_failed(self):
        resources = [
            {"url": {"value": "bad"}},
            {"url": ["bad"]},
            {"url": "https://[broken/file.pdf"},
        ]
        self.assertEqual(len(ai.deduplicate_resources(resources)), 3)
        self.assertEqual(
            [ai.resource_entry(resource)["status"] for resource in resources], ["failed"] * 3
        )

    def test_missing_video_url_is_still_an_explicit_skip(self):
        self.assertEqual(ai.resource_entry({"type": 22})["status"], "skipped_video")
        self.assertEqual(
            ai.resource_entry({"type": 22}, True)["status"], "skipped_unsupported_format"
        )

    def test_ebook_label_with_document_url_is_downloadable(self):
        entry = ai.resource_entry({"url": "https://example.invalid/file.pdf", "type": 21})
        self.assertEqual(entry["status"], "discovered")

    def test_default_skip_and_unsupported_formats(self):
        self.assertEqual(
            ai.resource_entry({"url": "https://example.invalid/video.mp4"})["status"],
            "skipped_video",
        )
        # MP4 gains a structural validator, so --include-video now downloads it.
        for suffix in ("mp4", "m4v"):
            entry = ai.resource_entry({"url": "https://example.invalid/file." + suffix}, True)
            self.assertEqual(entry["status"], "discovered")
        for suffix in ("doc", "xls", "ppt", "mkv", "avi"):
            entry = ai.resource_entry({"url": "https://example.invalid/file." + suffix}, True)
            self.assertEqual(entry["status"], "skipped_unsupported_format")

    def test_video_candidates_prefer_original_then_transcode(self):
        original = "https://example.invalid/a/b/hash.mp4"
        self.assertEqual(
            ai.download_candidates(original),
            [(original, "original"), ("https://example.invalid/a/b/hash_512.mp4", "transcoded")],
        )
        # Non-video resources have exactly one candidate.
        self.assertEqual(
            ai.download_candidates("https://example.invalid/file.pdf"),
            [("https://example.invalid/file.pdf", "original")],
        )

    def test_transcode_fallback_records_variant_and_keeps_original_identity(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            args = SimpleNamespace(out=temporary, include_video=True, list_only=False)
            calls = []

            def fake_download(url, dest, **kwargs):
                calls.append(url)
                if "_512" not in url:
                    raise ValueError("HTTP 424")
                with open(dest, "wb") as stream:
                    stream.write(b"\x00\x00\x00\x10ftypisom" + b"\x00" * 8)
                return 24

            with (
                patch.object(ai, "download_atomic", side_effect=fake_download),
                patch.object(ai, "is_usable", return_value=True),
            ):
                entries = ai.download_resources(
                    [{"url": "https://example.invalid/v.mp4", "name": "v.mp4"}], args, {}
                )
            self.assertEqual(entries[0]["status"], "ok")
            self.assertEqual(entries[0]["variant"], "transcoded")
            self.assertEqual(calls[0], "https://example.invalid/v.mp4")
            self.assertEqual(calls[1], "https://example.invalid/v_512.mp4")
            # Identity stays the original URL so a rerun recognises the file.
            self.assertEqual(entries[0]["file"], "v.mp4")

    def test_both_candidates_failing_is_reported_once(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            args = SimpleNamespace(out=temporary, include_video=True, list_only=False)
            with patch.object(ai, "download_atomic", side_effect=ValueError("HTTP 403")):
                entries = ai.download_resources(
                    [{"url": "https://example.invalid/v.mp4", "name": "v.mp4"}], args, {}
                )
            self.assertEqual(entries[0]["status"], "failed")
            self.assertEqual(entries[0]["error"], "HTTP 403")
            self.assertNotIn("variant", entries[0])

    def test_failed_download_keeps_resource_and_redacts_error(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            args = SimpleNamespace(out=temporary, include_video=False, list_only=False)
            with patch.object(
                ai,
                "download_atomic",
                side_effect=OSError("https://example.invalid/file.pdf?token=secret"),
            ):
                entries = ai.download_resources(
                    [{"url": "https://example.invalid/file.pdf"}], args, {}
                )
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "failed")
            self.assertNotIn("secret", entries[0]["error"])

    def test_interrupted_download_is_checkpointed(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            args = SimpleNamespace(out=temporary, include_video=False, list_only=False)
            entries, snapshots = [], []
            with patch.object(ai, "download_atomic", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    ai.download_resources(
                        [{"url": "https://example.invalid/file.pdf"}],
                        args,
                        {},
                        entries,
                        lambda: snapshots.append(entries[0]["status"]),
                    )
            self.assertEqual(snapshots, ["failed"])

    def test_browser_start_error_returns_nonzero_and_manifest(self):
        with tempfile.TemporaryDirectory(dir=".") as temporary:
            state = Path(temporary, "state.json")
            state.write_text("{}", encoding="utf-8")
            with (
                patch.object(
                    ai, "sync_playwright", side_effect=RuntimeError("Browser unavailable")
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = ai.main(
                    [
                        "--course-id",
                        "course",
                        "--class-id",
                        "class",
                        "--out",
                        temporary,
                        "--state",
                        str(state),
                    ]
                )
            self.assertEqual(code, 1)
            manifest = json.loads(Path(temporary, "_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["tasks"][0]["status"], "failed")
            # Final publication happened before lock release; another run can now acquire it.
            lock = ai.acquire_lock(temporary)
            self.assertIsNotNone(lock)
            ai.release_lock(lock)

    def test_list_only_statuses_never_access_network(self):
        args = SimpleNamespace(out="unused", include_video=False, list_only=True)
        resources = [
            {"url": "https://example.invalid/a.pdf"},
            {"url": "https://example.invalid/b.mp4"},
            {"type": 21},
            {"type": 11},
        ]
        with patch.object(ai, "download_atomic") as download:
            entries = ai.download_resources(resources, args, {})
        download.assert_not_called()
        self.assertEqual(
            [row["status"] for row in entries],
            ["discovered", "skipped_video", "skipped_ebook", "failed"],
        )


if __name__ == "__main__":
    unittest.main()
