import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from health_services import HealthScanCancelled, scan_data_health
from photo_classifier import OTHER, PhotoPrediction


def predicted(category, confidence):
    others = [c for c in (1, 2, 3, 4, 5, OTHER) if c != category]
    probabilities = {c: (1.0 - confidence) / len(others) for c in others}
    probabilities[category] = confidence
    return PhotoPrediction.from_probabilities(list(probabilities), list(probabilities.values()))


class FakeClassifier:
    """Answers by file name; an Exception value is raised instead."""

    def __init__(self, by_name, on_predict=None):
        self.by_name = dict(by_name)
        self.on_predict = on_predict
        self.samples = []

    def predict(self, sample):
        self.samples.append(Path(sample).name)
        if self.on_predict is not None:
            self.on_predict(sample)
        result = self.by_name[Path(sample).name]
        if isinstance(result, Exception):
            raise result
        return result


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

    def make_box(self, root, box_name, slots):
        """slots: {category: [file names]}; every file is created on disk."""
        box_dir = root / box_name
        box_dir.mkdir()
        for names in slots.values():
            for name in names:
                (box_dir / name).write_bytes(name.encode())
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8")) if (root / "meta.json").exists() else {}
        meta[box_name] = {str(category): list(names) for category, names in slots.items()}
        (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return box_dir

    def suspected(self, report):
        return {
            (issue.task_name, issue.detail.split(" ", 1)[0]): issue
            for issue in report.issues
            if issue.code == "suspected_wrong_category"
        }

    def test_classifier_flags_confidently_misplaced_photos_as_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box = "OOLU0431535"
            self.make_box(root, box, {c: [f"{box}-{c}.jpg"] for c in range(1, 9)})
            self.make_box(root, "LYGU3287520", {1: ["LYGU3287520-1.jpg"]})
            classifier = FakeClassifier({
                f"{box}-1.jpg": predicted(1, 0.99),
                f"{box}-2.jpg": predicted(3, 0.90),  # disagrees, but not sure enough
                f"{box}-3.jpg": predicted(OTHER, 0.99),
                f"{box}-4.jpg": predicted(OTHER, 0.99),
                f"{box}-5.jpg": predicted(4, 0.97),
                f"{box}-6.jpg": predicted(OTHER, 0.99),
                f"{box}-7.jpg": predicted(4, 0.99),
                f"{box}-8.jpg": predicted(5, 0.60),
                "LYGU3287520-1.jpg": predicted(1, 1.0),
            })
            labels = {3: "全开门满箱", 4: "半关门满箱", 5: "全关门"}

            report = scan_data_health(root, photo_classifier=classifier, category_labels=labels)

            found = self.suspected(report)
            self.assertEqual(
                set(found),
                {(box, f"{box}-3.jpg"), (box, f"{box}-4.jpg"), (box, f"{box}-5.jpg"), (box, f"{box}-7.jpg")},
            )
            self.assertIn(
                "放在 3 类（全开门满箱），看起来像 6-8 类（加固/底梁/铅封）（模型把握 99%）",
                found[(box, f"{box}-3.jpg")].detail,
            )
            self.assertIn("放在 5 类（全关门），看起来像 4 类（半关门满箱）", found[(box, f"{box}-5.jpg")].detail)
            self.assertIn("放在 6-8 类（加固/底梁/铅封），看起来像 4 类（半关门满箱）", found[(box, f"{box}-7.jpg")].detail)
            self.assertTrue(all(issue.severity == "warning" for issue in found.values()))
            self.assertEqual(found[(box, f"{box}-3.jpg")].title, "疑似放错类别")
            self.assertEqual(report.error_count, 0)
            self.assertEqual(report.classified_photo_count, 9)
            self.assertIn("模型核对照片类别：9 张", report.to_text())
            self.assertIn(f"警告 [{box}] 疑似放错类别：{box}-7.jpg 放在 6-8 类", report.to_text())

    def test_category_threshold_is_adjustable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_box(root, "LYGU3043781", {2: ["half.jpg"]})
            classifier = FakeClassifier({"half.jpg": predicted(3, 0.90)})

            default = scan_data_health(root, photo_classifier=classifier)
            lowered = scan_data_health(root, photo_classifier=classifier, category_threshold=0.85)

            self.assertEqual(self.suspected(default), {})
            self.assertEqual(
                self.suspected(lowered)[("LYGU3043781", "half.jpg")].detail,
                "half.jpg 放在 2 类，看起来像 3 类（模型把握 90%）",
            )

    def test_category_check_skips_missing_and_unreadable_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_dir = self.make_box(root, "LYGU3043781", {1: ["gone.jpg"], 2: ["broken.jpg"], 3: ["full.jpg"]})
            (box_dir / "gone.jpg").unlink()
            classifier = FakeClassifier({
                "broken.jpg": OSError("cannot identify image file"),
                "full.jpg": predicted(OTHER, 0.99),
            })

            report = scan_data_health(root, photo_classifier=classifier)
            without_model = scan_data_health(root)

            self.assertNotIn("gone.jpg", classifier.samples)
            self.assertIn("broken.jpg", classifier.samples)
            self.assertEqual(set(self.suspected(report)), {("LYGU3043781", "full.jpg")})
            self.assertEqual(report.classified_photo_count, 1)
            self.assertEqual(report.error_count, 0)
            self.assertIn("missing_photo_reference", {issue.code for issue in report.issues})
            self.assertEqual(self.suspected(without_model), {})
            self.assertEqual(without_model.classified_photo_count, 0)
            self.assertNotIn("模型核对", without_model.to_text())

    def test_category_check_can_be_cancelled_between_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_box(root, "LYGU3043781", {1: ["a.jpg"], 2: ["b.jpg"], 3: ["c.jpg"]})
            cancel_event = threading.Event()
            classifier = FakeClassifier(
                {name: predicted(1, 0.99) for name in ("a.jpg", "b.jpg", "c.jpg")},
                on_predict=lambda _sample: cancel_event.set(),
            )

            with self.assertRaises(HealthScanCancelled):
                scan_data_health(root, photo_classifier=classifier, cancel_event=cancel_event)

            self.assertEqual(classifier.samples, ["a.jpg"])


if __name__ == "__main__":
    unittest.main()
