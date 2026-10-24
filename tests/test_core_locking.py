"""Cross-process exclusion and automatic crash release, on Windows and POSIX."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from course_resource_download.locking import (
    acquire_lock,
    cleanup_stale_lock_files,
    cleanup_stale_parts,
    release_lock,
)


def read_ready(child):
    """Bound readiness waits even if a locking regression hangs the child."""
    messages = queue.Queue()
    reader = threading.Thread(target=lambda: messages.put(child.stdout.readline()), daemon=True)
    reader.start()
    return messages.get(timeout=10).strip()


class KernelLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.env = dict(os.environ)
        src = Path(__file__).resolve().parents[1] / "src"
        self.env["PYTHONPATH"] = str(src) + os.pathsep + self.env.get("PYTHONPATH", "")

    def test_process_exclusion_persistent_filename_and_crash_recovery(self):
        lock = acquire_lock(self.out)
        self.assertIsNotNone(lock)
        self.addCleanup(release_lock, lock)
        self.assertIsNone(acquire_lock(self.out))
        child_code = "from course_resource_download.locking import acquire_lock; import sys; print('busy' if acquire_lock(sys.argv[1]) is None else 'acquired', flush=True)"
        result = subprocess.run(
            [sys.executable, "-c", child_code, str(self.out)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "busy")
        # Old timestamps and stale recovery must not break a live kernel lock.
        lock_path = self.out / ".download.lock"
        os.utime(lock_path, (1, 1))
        self.assertEqual(cleanup_stale_lock_files(self.out, 0), 0)
        self.assertTrue(lock_path.exists())
        release_lock(lock)
        release_lock(lock)
        self.assertTrue(lock_path.exists())
        child_code = "from course_resource_download.locking import acquire_lock; import sys; lock=acquire_lock(sys.argv[1]); print('acquired' if lock else 'busy', flush=True); sys.stdin.readline()"
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.out)],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(read_ready(child), "acquired")
            self.assertIsNone(acquire_lock(self.out))
            child.kill()
            child.communicate(timeout=10)
            recovered = acquire_lock(self.out)
            self.assertIsNotNone(recovered)
            release_lock(recovered)
            self.assertTrue(lock_path.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)

    def test_cleanup_removes_only_our_old_parts(self):
        old = self.out / ".crd-download-old.part"
        recent = self.out / ".crd-download-new.part"
        unrelated = self.out / "my-project.part"
        for path in (old, recent, unrelated):
            path.write_bytes(b"partial")
        os.utime(old, (1, 1))
        os.utime(unrelated, (1, 1))
        self.assertEqual(cleanup_stale_parts(self.out), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())

    def test_busy_cleanup_does_not_touch_other_process_staging(self):
        staging = self.out / ".crd-download-active.part"
        staging.write_bytes(b"partial")
        os.utime(staging, (1, 1))
        code = "from course_resource_download.locking import acquire_lock; import sys; lock=acquire_lock(sys.argv[1]); print(bool(lock), flush=True); sys.stdin.readline()"
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.out)],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(read_ready(child), "True")
            self.assertEqual(cleanup_stale_parts(self.out, 0), 0)
            self.assertTrue(staging.exists())
        finally:
            child.kill()
            child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
