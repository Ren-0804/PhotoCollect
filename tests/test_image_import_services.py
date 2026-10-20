import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from image_import_services import (
    ImageImportSource,
    import_task_images,
)
from metadata_services import read_metadata


class ImageImportServiceTests(unittest.TestCase):
    def test_local_files_are_hashed_copied_and_committed_in_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            source_dir = root / "incoming"
            source_dir.mkdir()
            first = source_dir / "first.jpg"
            second = source_dir / "second.png"
            first.write_bytes(b"first-photo")
            second.write_bytes(b"second-photo")

            summary = import_task_images(
                root,
                box_name,
                4,
                [
                    ImageImportSource.from_path(first),
                    ImageImportSource.from_path(second),
                ],
            )

            self.assertEqual(
                summary.added_names,
                ("LYGU3043781-4.jpg", "LYGU3043781-4.png"),
            )
            self.assertEqual(summary.duplicate_count, 0)
            meta = read_metadata(root / "meta.json")
            self.assertEqual(meta[box_name]["4"], list(summary.added_names))
            self.assertEqual(
                (root / box_name / summary.added_names[0]).read_bytes(),
                b"first-photo",
            )

    def test_duplicate_payload_is_skipped_without_a_second_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            source = ImageImportSource.from_payload("clipboard", b"same-jpeg")

            first = import_task_images(root, box_name, 4, [source])
            second = import_task_images(root, box_name, 5, [source])

            self.assertEqual(len(first.added_names), 1)
            self.assertEqual(second.added_names, ())
            self.assertEqual(second.duplicate_count, 1)
            self.assertEqual(
                len(list((root / box_name).glob("*.jpg"))),
                1,
            )

    def test_metadata_failure_rolls_back_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            source = root / "incoming.jpg"
            source.write_bytes(b"new-photo")

            with patch(
                "image_import_services.update_metadata",
                side_effect=OSError("metadata unavailable"),
            ):
                with self.assertRaises(OSError):
                    import_task_images(
                        root,
                        box_name,
                        4,
                        [ImageImportSource.from_path(source)],
                    )

            self.assertEqual(list(box_dir.iterdir()), [])

    def test_import_work_can_run_entirely_outside_the_caller_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            source = root / "network-photo.jpg"
            source.write_bytes(b"network-photo")
            caller_thread = threading.get_ident()
            progress_threads = []
            result = []

            worker = threading.Thread(
                target=lambda: result.append(
                    import_task_images(
                        root,
                        box_name,
                        5,
                        [ImageImportSource.from_path(source)],
                        progress_callback=lambda *_args: progress_threads.append(
                            threading.get_ident()
                        ),
                    )
                )
            )
            worker.start()
            worker.join(5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result[0].added_names), 1)
            self.assertTrue(progress_threads)
            self.assertNotEqual(progress_threads[0], caller_thread)


if __name__ == "__main__":
    unittest.main()
