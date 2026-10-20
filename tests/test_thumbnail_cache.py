import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from con import ImageLoadTask, ThumbnailCache, _LoadSignals


class ThumbnailCacheTests(unittest.TestCase):
    def test_cancelled_queued_loader_does_not_read_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"not-needed")
            task = ImageLoadTask(str(path), QSize(80, 60), _LoadSignals())

            task.cancel()

            with unittest.mock.patch.object(Path, "read_bytes") as read_bytes:
                task.run()
            read_bytes.assert_not_called()

    def test_memory_budget_evicts_oldest_qimages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(3):
                path = root / f"{index}.jpg"
                path.write_bytes(str(index).encode())
                paths.append(path)
            image = QImage(100, 100, QImage.Format_RGB32)
            image.fill(0)
            single_bytes = image.sizeInBytes()
            cache = ThumbnailCache(max_size=10, max_bytes=single_bytes * 2)

            for path in paths:
                cache.put(str(path), QSize(100, 100), image)

            self.assertEqual(cache.count, 2)
            self.assertLessEqual(cache.bytes_used, single_bytes * 2)
            self.assertIsNone(cache.get(str(paths[0]), QSize(100, 100)))
            self.assertIsNotNone(cache.get(str(paths[-1]), QSize(100, 100)))

    def test_changed_file_replaces_stale_entry_for_the_same_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"first")
            cache = ThumbnailCache(max_size=10, max_bytes=1024 * 1024)
            size = QSize(80, 60)
            first = QImage(80, 60, QImage.Format_RGB32)
            first.fill(0)
            cache.put(str(path), size, first)
            original_mtime = path.stat().st_mtime

            path.write_bytes(b"second")
            os.utime(path, (original_mtime + 2, original_mtime + 2))
            second = QImage(80, 60, QImage.Format_RGB32)
            second.fill(1)
            cache.put(str(path), size, second)

            self.assertEqual(cache.count, 1)
            self.assertEqual(cache.get(str(path), size).pixel(0, 0), second.pixel(0, 0))

    def test_concurrent_workers_can_get_put_and_trim_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(24):
                path = root / f"{index}.jpg"
                path.write_bytes(str(index).encode())
                paths.append(path)
            cache = ThumbnailCache(max_size=16, max_bytes=4 * 1024 * 1024)
            size = QSize(64, 64)

            def exercise(path):
                image = QImage(64, 64, QImage.Format_RGB32)
                image.fill(3)
                cache.put(str(path), size, image)
                cache.get(str(path), size)
                cache.trim_to_fraction(0.75)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(exercise, paths))

            self.assertLessEqual(cache.count, 16)
            self.assertLessEqual(cache.bytes_used, 4 * 1024 * 1024)

    def test_trim_releases_unused_capacity_without_clearing_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = ThumbnailCache(max_size=8, max_bytes=8 * 1024 * 1024)
            size = QSize(100, 100)
            image = QImage(100, 100, QImage.Format_RGB32)
            for index in range(8):
                path = root / f"{index}.jpg"
                path.write_bytes(b"x")
                cache.put(str(path), size, image)

            cache.trim_to_fraction(0.25)

            self.assertLessEqual(cache.count, 2)
            self.assertGreater(cache.count, 0)


if __name__ == "__main__":
    unittest.main()
