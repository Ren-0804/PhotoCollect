import tempfile
import unittest
from pathlib import Path

from task_services import build_export_preflight, resolve_task_photos


class TaskPhotoResolutionTests(unittest.TestCase):
    def test_resolution_uses_existing_in_task_files_and_deduplicates_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_dir = root / "LYGU3043781"
            box_dir.mkdir()
            outside = root / "outside.jpg"
            outside.write_bytes(b"outside")
            (box_dir / "same.jpg").write_bytes(b"same")
            (box_dir / "third.jpg").write_bytes(b"third")
            meta = {
                "1": ["same.jpg", "missing.jpg", "../outside.jpg"],
                "2": ["same.jpg"],
                "3": ["third.jpg"],
                "__requirements__": [1, 2, 3],
            }

            state = resolve_task_photos(
                box_dir,
                meta,
                global_requirements=[1],
                category_count=8,
                auto_ocr_categories={4, 5},
            )

            self.assertEqual(state.photo_count, 2)
            self.assertEqual(state.source_photo_bytes, len(b"same") + len(b"third"))
            self.assertEqual(state.category_counts[:3], (1, 0, 1))
            self.assertEqual(state.required_categories, (1, 2, 3))
            self.assertEqual(state.missing_category_ids, (2,))
            self.assertEqual(
                [(photo.category, photo.filename) for photo in state.photos],
                [(1, "same.jpg"), (3, "third.jpg")],
            )

    def test_resolution_falls_back_to_valid_global_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            box_dir = Path(tmp) / "MSCU6639870"
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door")
            meta = {
                "1": ["door.jpg"],
                "__requirements__": "invalid",
            }

            state = resolve_task_photos(
                box_dir,
                meta,
                global_requirements=[1, 5, 5, 9, "2"],
                category_count=8,
                auto_ocr_categories={4, 5},
            )

            self.assertEqual(state.required_categories, (1, 5))
            self.assertEqual(state.missing_category_ids, (5,))
            self.assertFalse(state.has_auto_ocr_photo)

    def test_resolution_marks_existing_ocr_category_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            box_dir = Path(tmp) / "MSCU6639870"
            box_dir.mkdir()
            (box_dir / "closed.jpg").write_bytes(b"closed")

            state = resolve_task_photos(
                box_dir,
                {"5": ["closed.jpg"]},
                global_requirements=[5],
                category_count=8,
                auto_ocr_categories={4, 5},
            )

            self.assertTrue(state.has_auto_ocr_photo)
            self.assertEqual(state.missing_category_ids, ())

    def test_resolution_recovers_unregistered_app_named_closed_door_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            box_dir = Path(tmp) / "LYGU3043781"
            box_dir.mkdir()
            (box_dir / "LYGU3043781-5.jpg").write_bytes(b"closed-door")
            (box_dir / "manual-photo.jpg").write_bytes(b"unknown-category")

            state = resolve_task_photos(
                box_dir,
                {"5": []},
                global_requirements=[5],
                category_count=8,
                auto_ocr_categories={4, 5},
            )

            self.assertEqual(
                [(photo.category, photo.filename) for photo in state.photos],
                [(5, "LYGU3043781-5.jpg")],
            )
            self.assertEqual(state.category_counts[4], 1)
            self.assertEqual(state.missing_category_ids, ())
            self.assertTrue(state.has_auto_ocr_photo)


class ExportPreflightTests(unittest.TestCase):
    def test_preflight_counts_selected_task_summaries(self):
        preflight = build_export_preflight(
            [
                {"task_state": "complete", "pic_cnt": 8},
                {"task_state": "pending", "pic_cnt": 3},
                {"task_state": "anomaly", "pic_cnt": 5},
            ],
            kind="zip",
            scope="全部任务",
            source_photo_bytes=12_345_678,
        )

        self.assertEqual(preflight.task_count, 3)
        self.assertEqual(preflight.complete_count, 1)
        self.assertEqual(preflight.pending_count, 1)
        self.assertEqual(preflight.anomaly_count, 1)
        self.assertEqual(preflight.photo_count, 16)
        self.assertEqual(preflight.source_photo_bytes, 12_345_678)


if __name__ == "__main__":
    unittest.main()
