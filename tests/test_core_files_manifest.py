"""Portable ownership and crash-safe manifest regression tests, without a site."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from course_resource_download import files, manifest


class FilesAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_names_are_portable_even_for_long_unicode_and_bad_remote_values(self):
        for source in [
            None,
            [],
            17,
            "CON.pdf",
            "aux.notes.txt",
            "../../bad:name.pdf",
            "..",
            "a." + "x" * 300,
            "讲义" * 100 + ".pdf",
            "surrogate\ud800.txt",
        ]:
            with self.subTest(source=source):
                name = files.safe_name(source)
                self.assertIsNotNone(files.safe_join(self.out, name))
                self.assertLessEqual(len(name.encode("utf-8")), files.MAX_NAME)
        self.assertEqual(files.safe_name("..", fallback=".."), "unnamed")

    def test_manifest_path_rejects_windows_and_posix_escape(self):
        for name in [
            "../a.pdf",
            "/a.pdf",
            r"..\a.pdf",
            r"C:\a.pdf",
            "CON.txt",
            "a.pdf.",
            "a.pdf ",
            "notes.txt:stream",
            "bad\0.pdf",
            9,
            [],
            "",
            "surrogate\ud800.txt",
        ]:
            with self.subTest(name=name):
                self.assertIsNone(files.safe_join(self.out, name))

    def test_unique_unicode_collisions_keep_suffix_and_original(self):
        name = files.safe_name("讲义" * 100 + ".txt")
        first = self.out / name
        first.write_text("original", encoding="utf-8")
        second = Path(files.unique_path(self.out, name, "source"))
        second.write_text("second", encoding="utf-8")
        third = Path(files.unique_path(self.out, name, "source"))
        self.assertNotIn(third, [first, second])
        self.assertIn("source_2", third.name)
        self.assertEqual(first.read_text(encoding="utf-8"), "original")

    def test_symlink_escape_and_reuse_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as elsewhere:
            target = Path(elsewhere) / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            try:
                (self.out / "link.txt").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable")
            self.assertIsNone(files.safe_join(self.out, "link.txt"))
            self.assertFalse(files.is_usable(self.out / "link.txt"))

    def test_claim_checks_contents_and_never_adopts_unowned_existing_name(self):
        (self.out / "notes.txt").write_text("owned by somebody else", encoding="utf-8")
        url = "https://example.test/course/notes"
        path, reused = files.claim_or_allocate(self.out, "notes.txt", url, {})
        self.assertFalse(reused)
        self.assertNotEqual(Path(path).name, "notes.txt")
        Path(path).write_text("proper UTF-8 content", encoding="utf-8")
        index = {url: Path(path).name}
        self.assertEqual(files.claim_or_allocate(self.out, "renamed.txt", url, index), (path, True))
        Path(path).write_bytes(b"\0corrupt")
        replacement, reused = files.claim_or_allocate(self.out, "notes.txt", url, index)
        self.assertFalse(reused)
        self.assertEqual(replacement, path)

    def test_discovery_and_partial_runs_preserve_prior_ownership(self):
        old = [{"url": "https://example.test/old", "file": "old.txt", "status": "ok"}]
        manifest.write_manifest(self.out, old)
        before = (self.out / "_manifest.json").read_bytes()
        manifest.write_manifest(self.out, [{"url": "new", "status": "discovered"}], list_only=True)
        self.assertEqual((self.out / "_manifest.json").read_bytes(), before)
        manifest.write_manifest(
            self.out,
            [
                {"url": "https://example.test/new", "file": "new.txt", "status": "exists"},
                {"url": old[0]["url"], "status": "failed"},
            ],
        )
        self.assertEqual(
            manifest.load_manifest_index(self.out),
            {old[0]["url"]: "old.txt", "https://example.test/new": "new.txt"},
        )
        data = json.loads((self.out / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["statistics"], {"exists": 1, "failed": 1})

    def test_atomic_replace_and_serialization_failure_preserve_report(self):
        manifest.write_manifest(self.out, [{"status": "ok"}])
        before = (self.out / "_manifest.json").read_bytes()
        with patch.object(manifest.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                manifest.write_manifest(self.out, [{"status": "failed"}])
        with self.assertRaises(TypeError):
            manifest.write_manifest(self.out, [{"status": "ok", "bad": {1, 2}}])
        self.assertEqual((self.out / "_manifest.json").read_bytes(), before)
        self.assertEqual(list(self.out.glob("*.part")), [])

    def test_legacy_and_malformed_manifests_never_escape_or_confuse_owners(self):
        samples = [
            None,
            17,
            [],
            {"resources": 17},
            {"ownership": [1]},
            {
                "resources": [
                    17,
                    None,
                    {"url": [], "file": "a.txt", "status": "ok"},
                    {"url": "escape", "file": "../a", "status": "ok"},
                ]
            },
        ]
        for sample in samples:
            (self.out / "_manifest.json").write_text(json.dumps(sample), encoding="utf-8")
            self.assertEqual(manifest.load_manifest_index(self.out), {})
        (self.out / "_manifest.json").write_text("not JSON", encoding="utf-8")
        self.assertEqual(manifest.load_manifest_index(self.out), {})
        rows = [
            {"url": "a", "file": "same.txt", "status": "ok"},
            {"url": "b", "file": "Same.txt", "status": "exists"},
        ]
        (self.out / "_manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        self.assertEqual(manifest.load_manifest_index(self.out), {})
        (self.out / "_manifest.json").write_text(json.dumps(rows[:1]), encoding="utf-8")
        self.assertEqual(manifest.load_manifest_index(self.out), {"a": "same.txt"})

    def test_every_record_is_accounted_and_unknowns_are_failures(self):
        entries = [
            {"status": "ok"},
            {"status": "skipped_ebook"},
            {"status": "skipped_non_document"},
            {"status": "skipped_unsupported_format"},
            {"status": "discovered"},
        ]
        self.assertFalse(manifest.summarize(entries)[1])
        entries.extend([None, {"status": []}, {"status": "made-up"}, {"status": "failed"}])
        counts, failed = manifest.summarize(entries)
        self.assertEqual(sum(counts.values()), len(entries))
        self.assertEqual(counts["unknown"], 3)
        self.assertTrue(failed)
        manifest.write_manifest(self.out, [None, {"status": "ok", "file": "../escape"}])
        data = json.loads((self.out / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(data["statistics"], {"unknown": 1, "failed": 1})


if __name__ == "__main__":
    unittest.main()
