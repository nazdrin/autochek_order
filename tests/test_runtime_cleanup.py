import os
import tempfile
import time
import unittest
from pathlib import Path

from services.runtime_cleanup import cleanup_startup, retain_sportatlet_sets


class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous_cleanup = os.environ.get("RUNTIME_CLEANUP_ON_START")
        os.environ["RUNTIME_CLEANUP_ON_START"] = "1"

    def tearDown(self):
        if self.previous_cleanup is None:
            os.environ.pop("RUNTIME_CLEANUP_ON_START", None)
        else:
            os.environ["RUNTIME_CLEANUP_ON_START"] = self.previous_cleanup
        self.tmp.cleanup()

    def _touch(self, relative: str, mtime: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        os.utime(path, (mtime, mtime))
        return path

    def test_startup_removes_debug_files_but_preserves_state(self):
        self._touch("artifacts/old.png", 1)
        self._touch("tmp/supplier2_debug/page.html", 1)
        self._touch("pages/page.png", 1)
        state = self._touch(".state_supplier2.json", 1)
        result = cleanup_startup(self.root)
        self.assertEqual(result.deleted_files, 3)
        self.assertTrue(state.exists())
        self.assertFalse((self.root / "artifacts/old.png").exists())

    def test_startup_removes_known_root_runtime_screenshots(self):
        screenshot = self._touch("supplier3_login_failed.png", 1)
        cleanup_startup(self.root)
        self.assertFalse(screenshot.exists())

    def test_retains_three_newest_labels(self):
        now = int(time.time())
        for index in range(5):
            self._touch(f"supplier4_labels/label-{index}.pdf", now + index)
        cleanup_startup(self.root)
        labels = sorted((self.root / "supplier4_labels").glob("*.pdf"))
        self.assertEqual([p.name for p in labels], ["label-2.pdf", "label-3.pdf", "label-4.pdf"])

    def test_sportatlet_pairs_are_removed_together(self):
        now = int(time.time())
        directory = self.root / "sportatlet_files"
        for index in range(4):
            self._touch(f"sportatlet_files/{index}.xlsx", now + index)
            self._touch(f"sportatlet_files/marking-{index}.pdf", now + index)
        retain_sportatlet_sets(self.root, directory, keep=3)
        self.assertFalse((directory / "0.xlsx").exists())
        self.assertFalse((directory / "marking-0.pdf").exists())
        self.assertTrue((directory / "1.xlsx").exists())

    def test_symlink_is_not_followed(self):
        outside = Path(self.tmp.name).parent / "runtime-cleanup-outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "artifacts" / "outside-link"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        cleanup_startup(self.root)
        self.assertTrue(outside.exists())
        outside.unlink()


if __name__ == "__main__":
    unittest.main()
