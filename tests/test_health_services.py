import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from health_services import HealthScanCancelled, scan_data_health


class DataHealthScanTests(unittest.TestCase):
    def test_clean_workspace_has_no_issues_and_ignores_internal_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door")
            internal = root / ".photocollect" / "trash"
            internal.mkdir(parents=True)
            (internal / "deleted.jpg").write_bytes(b"deleted")
            (root / "meta.json").write_text(json.dumps({
                "__requirements__": [1],
                box_name: {"1": ["door.jpg"]},
            }), encoding="utf-8")

            report = scan_data_health(
                root,
                task_name_validator=lambda name: name == box_name,
            )

            self.assertEqual(report.task_count, 1)
            self.assertEqual(report.referenced_photo_count, 1)
            self.assertEqual(report.internal_directory_count, 1)
            self.assertEqual(report.issues, ())

    def test_invalid_primary_metadata_uses_valid_backup_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door")
            primary = root / "meta.json"
            backup = root / "meta.json.bak"
            primary.write_text("{invalid", encoding="utf-8")
            backup.write_text(json.dumps({
                "__requirements__": [1],
                box_name: {"1": ["door.jpg"]},
            }), encoding="utf-8")
            before = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (primary, backup, box_dir / "door.jpg")
            }

            report = scan_data_health(root)

            after = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (primary, backup, box_dir / "door.jpg")
            }
            self.assertTrue(report.used_backup)
            self.assertIn("metadata_invalid", {issue.code for issue in report.issues})
            self.assertIn("metadata_backup_used", {issue.code for issue in report.issues})
            self.assertEqual(before, after)

    def test_scan_detects_stale_orphan_duplicate_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "INVALID-TASK"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "same.jpg").write_bytes(b"same")
            (box_dir / "orphan.png").write_bytes(b"orphan")
            (root / "outside.jpg").write_bytes(b"outside")
            (root / ".photocollect").mkdir()
            (root / "meta.json").write_text(json.dumps({
                box_name: {
                    "1": ["same.jpg", "missing.jpg", "../outside.jpg"],
                    "2": ["same.jpg"],
                    "3": "not-a-list",
                },
                "MISSING-DIRECTORY": {"1": []},
                ".photocollect": {"1": ["internal.jpg"]},
            }), encoding="utf-8")

            report = scan_data_health(
                root,
                task_name_validator=lambda _name: False,
            )
            codes = {issue.code for issue in report.issues}

            self.assertIn("invalid_task_name", codes)
            self.assertIn("missing_photo_reference", codes)
            self.assertIn("unsafe_photo_reference", codes)
            self.assertIn("duplicate_photo_reference", codes)
            self.assertIn("invalid_category_value", codes)
            self.assertIn("orphan_photo", codes)
            self.assertIn("missing_task_directory", codes)
            self.assertIn("internal_metadata_entry", codes)

    def test_scan_reports_read_or_write_permission_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("health_services.os.access", return_value=False):
                report = scan_data_health(root)

            codes = {issue.code for issue in report.issues}
            self.assertIn("root_not_readable", codes)
            self.assertIn("root_not_writable", codes)

    def test_scan_reports_current_task_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("LYGU3043781", "MSCU6639870", "CICU4847978"):
                (root / name).mkdir()
            (root / "meta.json").write_text("{}", encoding="utf-8")
            cancel_event = threading.Event()
            phases = []

            def on_progress(current, total, label):
                phases.append((current, total, label))
                if current == 2:
                    cancel_event.set()

            with self.assertRaises(HealthScanCancelled):
                scan_data_health(
                    root,
                    cancel_event=cancel_event,
                    progress_callback=on_progress,
                )

            self.assertEqual(phases[0][2], "读取工作目录")
            self.assertEqual(phases[1][2], "检查元数据")
            self.assertTrue(phases[2][2].startswith("检查任务 "))


if __name__ == "__main__":
    unittest.main()
