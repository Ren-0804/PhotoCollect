import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
)

import con


class _FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _AcceptedRequirementsDialog:
    selected = [1, 4, 5]

    def __init__(self, *args, **kwargs):
        self.reqs_changed = _FakeSignal()

    def exec(self):
        self.reqs_changed.callback(list(self.selected))
        return QDialog.Accepted


class CompletedFeatureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_patch = patch.object(
            con,
            "CONFIG_PATH",
            Path(self.config_dir.name) / "config.json",
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.config_dir.cleanup()

    @staticmethod
    def _make_root(base: Path, names=("LYGU3043781", "MSCU6639870")) -> Path:
        root = base / "work"
        root.mkdir()
        for name in names:
            (root / name).mkdir()
        con.write_meta(root / con.META_NAME, {"__requirements__": [1, 4, 5]})
        return root

    def _window_with_tasks(self, root: Path) -> con.ContainerApp:
        window = con.ContainerApp()
        window.root = root
        window.refresh()
        self.addCleanup(window.close)
        return window

    def test_ctrl_shift_selection_exposes_batch_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = self._window_with_tasks(self._make_root(Path(tmp)))

            self.assertEqual(
                window.list.selectionMode(),
                QAbstractItemView.ExtendedSelection,
            )
            window.list.item(0).setSelected(True)
            window.list.item(1).setSelected(True)
            QApplication.processEvents()

            self.assertEqual(len(window._selected_task_names()), 2)
            self.assertEqual(window.lbl_selection_count.text(), "已选 2 项")
            self.assertFalse(window.btn_selection_export.isHidden())
            self.assertFalse(window.btn_selection_reqs.isHidden())
            self.assertFalse(window.btn_selection_delete.isHidden())

    def test_batch_requirements_are_committed_in_one_metadata_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_root(Path(tmp))
            window = self._window_with_tasks(root)
            window.list.item(0).setSelected(True)
            window.list.item(1).setSelected(True)

            with patch.object(
                con,
                "BoxRequirementsDialog",
                _AcceptedRequirementsDialog,
            ):
                window.open_batch_requirements()

            metadata = con.read_meta(root / con.META_NAME)
            self.assertEqual(metadata["LYGU3043781"]["__requirements__"], [1, 4, 5])
            self.assertEqual(metadata["MSCU6639870"]["__requirements__"], [1, 4, 5])

    def test_selected_zip_export_uses_selected_count_in_custom_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_root(Path(tmp), ("A", "B", "C"))
            window = self._window_with_tasks(root)
            window.conf["zip_name_template"] = "【交接包】{YYYYMMDD}_{箱数}箱.zip"
            window.list.item(0).setSelected(True)
            window.list.item(2).setSelected(True)
            captured = []

            def confirm(preflight):
                captured.append(preflight)
                return True

            with (
                patch.object(window, "_confirm_export_preflight", side_effect=confirm),
                patch.object(
                    con.QFileDialog,
                    "getSaveFileName",
                    return_value=("", ""),
                ) as save_dialog,
            ):
                window.export_handoff("selected")

            self.assertEqual(captured[0].scope, "所选任务")
            self.assertEqual(captured[0].task_count, 2)
            default_path = save_dialog.call_args.args[2]
            self.assertIn("2箱.zip", default_path)

    def test_batch_create_and_ocr_edit_show_live_iso_check_digit(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = con.BatchCreateDialog(Path(tmp))
            prefix = "MSCU663987"
            complete = prefix + str(con.iso6346_check_digit(prefix))
            earlier = ("CSQU305438", "TGHU123456", "CMAU765432")
            batch.txt.setPlainText("\n".join((*earlier, prefix)))
            QApplication.processEvents()
            self.assertIn(complete, batch.preview.text())
            self.assertTrue(batch.preview.text().splitlines()[-1].startswith("校验位提示：…"))

            correction = con.OcrResultDialog({"container_no": prefix})
            self.assertIn(complete, correction.lbl_check_digit_hint.text())
            correction.close()
            batch.close()

    def test_theme_dialog_offers_five_functionally_isolated_palettes(self):
        dialog = con.ThemeChoiceDialog("graphite")
        self.addCleanup(dialog.close)
        self.assertEqual(len(dialog._radios), 5)
        self.assertEqual(dialog.selected_name(), "graphite")
        self.assertIn("只改变颜色", dialog.findChildren(con.QLabel)[1].text())

    def test_manual_batch_ocr_button_is_directly_below_auto_ocr(self):
        window = con.ContainerApp()
        self.addCleanup(window.close)
        sidebar_layout = window.sidebar.layout()
        self.assertEqual(
            sidebar_layout.indexOf(window.btn_batch_ocr),
            sidebar_layout.indexOf(window.ocr_toggle_row) + 1,
        )

    def test_inferred_ocr_ui_distinguishes_final_result_from_raw_text(self):
        result = {
            "container_no": "CICU7769210",
            "size_type": "25G1",
            "check_digit_valid": True,
            "inferred_check_digit": True,
            "verification_status": "inferred_match",
            "confidence": {"level": "medium"},
            "raw_lines": [
                {"text": "CICU", "score": 0.7141},
                {"text": "776921", "score": 0.7963},
                {"text": "25G1", "score": 0.7551},
            ],
        }
        dialog = con.OcrResultDialog(result)
        labels = [label.text() for label in dialog.findChildren(con.QLabel)]
        self.assertIn("解析后的最终结果（可确认或修正）", labels)
        self.assertIn("OCR 原始文本（仅供核对，不是最终箱号）", labels)
        self.assertEqual(dialog.edits["container_no"].text(), "CICU7769210")
        self.assertEqual(dialog.lbl_status.text(), "校验位需确认")
        dialog.close()

        row = con.TaskRowWidget({
            "name": "CICU7769210",
            "pic_cnt": 6,
            "missing": [],
            "ok": True,
            "task_state": "complete",
            "ocr_container": "CICU7769210",
            "ocr_size_type": "25G1",
            "ocr_status": "inferred_match",
            "box_number_valid": True,
        })
        row_texts = [label.text() for label in row.findChildren(con.QLabel)]
        self.assertIn("推算吻合", row_texts)
        self.assertNotIn("基本吻合", row_texts)
        self.assertIn("CICU7769210", row.verification_badge.toolTip())
        row.close()


class BackgroundWorkerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_qrunnable_image_import_commits_from_a_background_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            source = root / "source.jpg"
            image = QImage(64, 48, QImage.Format_RGB32)
            image.fill(QColor("#3578E5"))
            self.assertTrue(image.save(str(source), "JPG"))

            worker = con.ImageImportTask(
                root,
                box_name,
                4,
                [("path", source.name, str(source))],
            )
            thread = threading.Thread(target=worker.run)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive())

            metadata = con.read_meta(root / con.META_NAME)
            stored_names = metadata[box_name]["4"]
            self.assertEqual(len(stored_names), 1)
            self.assertTrue((root / box_name / stored_names[0]).is_file())

    def test_detail_page_queues_import_without_blocking_and_refreshes_after_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            source = root / "network-style-source.jpg"
            image = QImage(96, 64, QImage.Format_RGB32)
            image.fill(QColor("#16A085"))
            self.assertTrue(image.save(str(source), "JPG"))
            detail = con.BoxDetailWindow(root, box_name)
            self.addCleanup(detail.prepare_for_disposal)

            with patch.object(
                detail,
                "_use_background_image_import",
                return_value=True,
            ):
                started = time.perf_counter()
                detail.add_files(4, [str(source)])
                returned_ms = (time.perf_counter() - started) * 1000

            self.assertLess(returned_ms, 100)
            self.assertIsNotNone(detail._image_import_task)
            deadline = time.monotonic() + 10
            while detail._image_import_task is not None and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
            QApplication.processEvents()

            self.assertIsNone(detail._image_import_task)
            metadata = con.read_meta(root / con.META_NAME)
            self.assertEqual(len(metadata[box_name]["4"]), 1)
            self.assertEqual(len(detail.meta["4"]), 1)

    def test_batch_ocr_merges_two_door_photos_and_writes_only_important_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            paths = [root / box_name / "closed.jpg", root / box_name / "half.jpg"]
            for path in paths:
                path.write_bytes(b"fixture")
            runner = con.BatchOcrRunner(
                [{"name": box_name, "image_paths": tuple(paths)}],
                root,
            )
            finished = []
            runner.signals.finished.connect(finished.append)
            first = {
                "container_no": box_name,
                "size_type": "22G1",
                "max_gross_kg": None,
                "tare_kg": None,
                "payload_kg": None,
                "raw_lines": [],
            }
            second = {
                "container_no": "",
                "size_type": "",
                "max_gross_kg": 30480,
                "tare_kg": 2200,
                "payload_kg": 28280,
                "capacity_m3": 33.2,
                "raw_lines": [],
            }

            with patch.object(con, "run_ocr_image", side_effect=[first, second]) as ocr:
                runner.run()

            self.assertEqual(ocr.call_count, 2)
            self.assertEqual(finished[0]["success_count"], 1)
            result = con.read_meta(root / con.META_NAME)[box_name]["__ocr__"]
            self.assertEqual(result["container_no"], box_name)
            self.assertEqual(result["size_type"], "22G1")
            self.assertEqual(result["max_gross_kg"], 30480)
            self.assertEqual(result["tare_kg"], 2200)
            self.assertEqual(result["payload_kg"], 28280)


if __name__ == "__main__":
    unittest.main()
