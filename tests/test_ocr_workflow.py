import os
import csv
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtCore import Qt, QSize, QPoint, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractButton, QLabel, QProgressBar, QWidget

import con


@contextmanager
def qt_temporary_directory():
    """Keep Windows test files alive until Qt background readers finish."""

    with tempfile.TemporaryDirectory() as tmp:
        try:
            yield tmp
        finally:
            QThreadPool.globalInstance().waitForDone(5000)
            app = QApplication.instance()
            if app is not None:
                app.processEvents()


class OcrVerificationTests(unittest.TestCase):
    def test_weight_labels_cannot_be_spliced_into_container_number(self):
        rows = [
            {"text": "CICU", "score": 0.7526, "x": 0, "y": 0},
            {"text": "734197", "score": 0.8524, "x": 100, "y": 20},
            {"text": "PAYLOAD", "score": 0.8559, "x": 0, "y": 100},
            {"text": "62.350 1b", "score": 0.7325, "x": 100, "y": 120},
            {"text": "37.4", "score": 0.7625, "x": 200, "y": 140},
        ]

        candidates = con.build_container_candidates(rows)

        self.assertEqual(candidates[0]["container_no"], "CICU7341978")
        self.assertEqual(candidates[0]["source_text"], "CICU 734197")
        self.assertNotIn("ADGZ3501837", [item["container_no"] for item in candidates])

    def test_split_container_number_keeps_supported_ocr_corrections(self):
        rows = [
            {"text": "C1CU", "score": 0.78, "x": 0, "y": 0},
            {"text": "734197", "score": 0.84, "x": 100, "y": 20},
            {"text": "8", "score": 0.81, "x": 200, "y": 40},
        ]

        candidates = con.build_container_candidates(rows)

        self.assertEqual(candidates[0]["container_no"], "CICU7341978")
        self.assertTrue(candidates[0]["valid"])
        self.assertFalse(candidates[0]["inferred_check_digit"])

    def test_match_and_inferred_match_are_distinct(self):
        base = {
            "container_no": "LYGU3043781",
            "check_digit_valid": True,
            "size_type": "22G1",
        }
        exact = con.annotate_ocr_verification(base, "LYGU3043781", automatic=True)
        inferred = con.annotate_ocr_verification(
            {**base, "inferred_check_digit": True}, "LYGU3043781", automatic=True
        )

        self.assertEqual(exact["verification_status"], "match")
        self.assertEqual(inferred["verification_status"], "inferred_match")

    def test_mismatch_and_unreadable_are_reported(self):
        prefix = "MSCU663987"
        other = prefix + str(con.iso6346_check_digit(prefix))
        mismatch = con.annotate_ocr_verification(
            {"container_no": other, "check_digit_valid": True}, "LYGU3043781"
        )
        unreadable = con.annotate_ocr_verification(
            {"container_no": "", "check_digit_valid": False}, "LYGU3043781"
        )

        self.assertEqual(mismatch["verification_status"], "mismatch")
        self.assertEqual(unreadable["verification_status"], "unreadable")

    def test_container_number_can_be_extracted_from_task_name(self):
        self.assertEqual(
            con.extract_declared_container_no("任务-LYGU 304378-1"),
            "LYGU3043781",
        )

    def test_batch_input_is_normalized_deduplicated_and_classified(self):
        analysis = con.analyze_batch_input(
            " lygu 304378-1，临时 箱号;LYGU3043781\tMSCU6639870\n",
            {"MSCU6639870"},
        )

        self.assertEqual([item.name for item in analysis.creatable], ["LYGU3043781", "临时箱号"])
        self.assertEqual(analysis.valid_count, 1)
        self.assertEqual(analysis.warning_count, 1)
        self.assertEqual(analysis.duplicate_count, 1)
        self.assertEqual(analysis.existing_count, 1)

    def test_task_state_prioritizes_anomaly_then_pending_then_complete(self):
        self.assertEqual(con.classify_task_state("错误箱号", [], "", False), "anomaly")
        self.assertEqual(con.classify_task_state("LYGU3043781", ["关门"], "", False), "pending")
        self.assertEqual(con.classify_task_state("LYGU3043781", [], "mismatch", True), "anomaly")
        self.assertEqual(con.classify_task_state("LYGU3043781", [], "match", True), "complete")

    def test_dashboard_and_report_share_reconciled_photo_state(self):
        with qt_temporary_directory() as tmp:
            box_dir = Path(tmp) / "LYGU3043781"
            box_dir.mkdir()
            (box_dir / "same.jpg").write_bytes(b"same")
            box_meta = {
                "1": ["same.jpg"],
                "2": ["same.jpg"],
                "__requirements__": [1, 2],
            }

            summary = con.build_task_summary(
                box_dir,
                box_meta,
                [1, 2],
                require_ocr=False,
            )
            report = con.build_collection_report([summary], con.ALL_CATEGORIES)
            row = report.rows[0]

            self.assertEqual(summary["pic_cnt"], 1)
            self.assertEqual(summary["category_counts"]["1"], 1)
            self.assertEqual(summary["category_counts"]["2"], 0)
            self.assertEqual(summary["missing_category_ids"], [2])
            self.assertEqual(row.photo_count, 1)
            self.assertEqual(row.category_counts[:2], (1, 0))
            self.assertEqual(row.missing_category_ids, (2,))

    def test_dashboard_report_csv_and_json_share_task_counts(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "same.jpg").write_bytes(b"same")
            (box_dir / "third.jpg").write_bytes(b"third")
            meta = {
                "__requirements__": [1, 2, 3],
                box_name: {
                    "1": ["same.jpg"],
                    "2": ["same.jpg"],
                    "3": ["third.jpg"],
                },
            }
            con.write_meta(root / con.META_NAME, meta)
            summary = con.build_task_summary(
                box_dir,
                meta[box_name],
                meta["__requirements__"],
                require_ocr=False,
            )
            report_row = con.build_collection_report(
                [summary],
                con.ALL_CATEGORIES,
            ).rows[0]
            record = con.TaskExportRecord(
                name=summary["name"],
                size_type=summary["ocr_size_type"],
                ocr_status=summary["ocr_status"],
                task_status="待补",
                photo_count=summary["pic_cnt"],
                missing=tuple(summary["missing"]),
                last_updated=summary["last_updated"],
                required_categories=tuple(summary["required_categories"]),
                missing_category_ids=tuple(summary["missing_category_ids"]),
            )
            destination = root / "handoff.zip"
            con.export_handoff_package(
                root,
                destination,
                [record],
                dict(con.ALL_CATEGORIES),
            )

            with zipfile.ZipFile(destination) as archive:
                csv_row = next(csv.DictReader(io.StringIO(
                    archive.read("summary.csv").decode("utf-8-sig")
                )))
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                json_summary = exported[box_name]["__summary__"]

            self.assertEqual(summary["pic_cnt"], 2)
            self.assertEqual(report_row.photo_count, 2)
            self.assertEqual(int(csv_row["照片数"]), 2)
            self.assertEqual(json_summary["photo_count"], 2)
            self.assertEqual(summary["missing"], ["全开门1/2箱"])
            self.assertEqual(report_row.missing_labels, ("全开门1/2箱",))
            self.assertEqual(csv_row["缺失项"], "全开门1/2箱")
            self.assertEqual(json_summary["missing"], ["全开门1/2箱"])

    def test_real_sample_recognizes_container_number_and_size_type(self):
        fixture = Path(__file__).parent / "fixtures" / "container_sample.jpg"

        result = con.run_ocr_image(str(fixture))

        self.assertEqual(result["container_no"], "LYGU3043781")
        self.assertEqual(result["size_type"], "22G1")
        self.assertTrue(result["check_digit_valid"])

        with qt_temporary_directory() as tmp:
            recompressed = Path(tmp) / "clipboard-quality-88.jpg"
            recompressed.write_bytes(con.qimage_jpeg_bytes(QImage(str(fixture))))
            compressed_result = con.run_ocr_image(str(recompressed))
        self.assertEqual(compressed_result["container_no"], "LYGU3043781")
        self.assertEqual(compressed_result["size_type"], "22G1")

    def test_framed_check_digit_is_read_instead_of_inferred(self):
        import cv2
        import numpy as np

        # The full-image pass drops a digit printed inside its frame, as on
        # real doors; the targeted re-read must recover it.
        image = np.full((300, 900, 3), (140, 60, 20), np.uint8)
        font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 2.0, 5
        cv2.putText(image, "CICU", (60, 140), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        cv2.putText(image, "410260", (300, 140), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        (width, height), _ = cv2.getTextSize("410260", font, scale, thickness)
        frame_x = 300 + width + 20
        cv2.rectangle(image, (frame_x, 140 - height - 12), (frame_x + 52, 152), (255, 255, 255), 3)
        cv2.putText(image, "3", (frame_x + 6, 140), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
        cv2.putText(image, "25G1", (300, 230), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

        with qt_temporary_directory() as tmp:
            path = Path(tmp) / "framed-check-digit.png"
            path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
            result = con.run_ocr_image(str(path))

        self.assertEqual(result["container_no"], "CICU4102603")
        self.assertFalse(result["inferred_check_digit"])
        self.assertEqual(result["size_type"], "25G1")


class AutoOcrUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_full_closed_door_image_triggers_automatic_ocr(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            calls = []
            window.maybe_auto_ocr = lambda idx, names: calls.append((idx, list(names)))
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))

            window.add_image_data(5, image)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 5)
            self.assertTrue(calls[0][1][0].endswith(".jpg"))
            self.assertTrue((Path(tmp) / "LYGU3043781" / calls[0][1][0]).is_file())
            window.close()

    def test_thumbnail_transient_failure_retries_instead_of_staying_blank(self):
        with qt_temporary_directory() as tmp:
            box_dir = Path(tmp)
            photo = box_dir / "LYGU3043781-5.jpg"
            photo.write_bytes(b"temporarily unavailable")

            with patch.object(con.QThreadPool, "globalInstance") as global_pool:
                widget = con.ImageItemWidget(photo.name, box_dir)
                widget.on_loaded(None)

            global_pool.return_value.start.assert_called_once()
            self.assertTrue(widget._retry_timer.isActive())
            self.assertNotEqual(widget.lbl.text(), "无法加载")
            widget._retry_timer.stop()
            widget.cancel_async_load()
            widget.close()

    def test_background_import_refreshes_only_the_changed_category(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            task = object()
            window._image_import_task = task
            summary = con.ImageImportSummary(
                added_names=("LYGU3043781-5.jpg",),
                duplicate_names=(),
                duplicate_count=0,
                skipped_count=0,
                failed_count=0,
                cancelled=False,
                failures=(),
            )

            with patch.object(window, "reload_from_disk") as reload_from_disk, \
                    patch.object(window, "maybe_auto_ocr"), \
                    patch.object(window, "focus_next_missing"):
                window._on_image_import_finished(5, task, summary)

            reload_from_disk.assert_called_once_with(categories={5})
            window.close()

    def test_auto_ocr_switch_disables_only_automatic_recognition(self):
        with qt_temporary_directory() as tmp:
            host = QWidget()
            host.conf = {"auto_ocr_enabled": False}
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781", host)
            calls = []
            window.run_ocr_for_image = lambda name, automatic=False: calls.append((name, automatic))

            window.maybe_auto_ocr(5, ["closed-door.jpg"])
            window.run_ocr_for_image("closed-door.jpg", automatic=False)

            self.assertEqual(calls, [("closed-door.jpg", False)])
            window.close()
            host.close()

    def test_trusted_ocr_result_prevents_repeated_automatic_recognition(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            path = window.box_dir / 'closed.jpg'
            path.write_bytes(b'photo')
            info = path.stat()
            window.ocr_result = {
                "container_no": "LYGU3043781",
                "verification_status": "match",
                'source_image': path.name,
                'source_signatures': {path.name: [info.st_size, info.st_mtime_ns, info.st_ctime_ns]},
            }
            calls = []
            window.run_ocr_for_image = lambda name, automatic=False: calls.append((name, automatic))

            window.maybe_auto_ocr(5, ["closed.jpg"])
            window.maybe_auto_ocr(5, ["closed.jpg"])

            self.assertEqual(calls, [])
            window.close()

    def test_automatic_ocr_queue_recognizes_latest_image_even_after_old_match(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window._ocr_task = object()

            window.maybe_auto_ocr(4, ["first.jpg"])
            window.maybe_auto_ocr(5, ["latest.jpg"])

            self.assertEqual(window._pending_auto_ocr, (5, "latest.jpg", False))
            window.ocr_result = {
                "container_no": "LYGU3043781",
                "verification_status": "match",
            }
            calls = []
            window.run_ocr_for_image = lambda name, automatic=False: calls.append((name, automatic))
            window._finish_ocr_task()

            self.assertEqual(calls, [('latest.jpg', True)])
            self.assertIsNone(window._pending_auto_ocr)
            window.close()

    def test_disabling_auto_ocr_does_not_hide_a_known_container_mismatch(self):
        self.assertEqual(
            con.classify_task_state(
                "LYGU3043781",
                [],
                "mismatch",
                True,
                require_ocr=False,
            ),
            "anomaly",
        )

    def test_clipboard_image_is_saved_as_compact_jpeg(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            fixture = Path(__file__).parent / "fixtures" / "container_sample.jpg"
            image = QImage(str(fixture))
            png_size = len(window._qimage_png_bytes(image))

            window.add_image_data(1, image)

            saved_name = window.meta["1"][0]
            saved_path = window.box_dir / saved_name
            self.assertEqual(saved_path.suffix.lower(), ".jpg")
            self.assertLess(saved_path.stat().st_size, png_size * 0.5)
            window.close()
            con.QThreadPool.globalInstance().waitForDone(5000)
            QApplication.processEvents()

    def test_home_row_shows_box_type_and_verification_status(self):
        row = con.TaskRowWidget({
            "name": "LYGU3043781",
            "pic_cnt": 2,
            "missing": [],
            "ok": True,
            "ocr_container": "LYGU3043781",
            "ocr_size_type": "22G1",
            "ocr_status": "match",
        })
        texts = [label.text() for label in row.findChildren(QLabel)]

        self.assertTrue(any("LYGU3043781" in text and "22G1" in text for text in texts))
        self.assertIn("箱号一致", texts)
        row.close()

    def test_home_row_uses_review_badge_for_unprocessed_ocr(self):
        row = con.TaskRowWidget({
            "name": "LYGU3043781",
            "pic_cnt": 4,
            "missing": [],
            "task_state": "anomaly",
            "ocr_status": "",
            "box_number_valid": True,
            "has_auto_ocr_photo": True,
        })
        texts = [label.text() for label in row.findChildren(QLabel)]

        self.assertIn("待核验", texts)
        self.assertNotIn("异常", texts)
        row.close()

    def test_disabled_auto_ocr_does_not_turn_a_complete_task_into_an_anomaly(self):
        with qt_temporary_directory() as tmp:
            box_dir = Path(tmp) / "LYGU3043781"
            box_dir.mkdir()
            image_path = box_dir / "closed.jpg"
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))
            image.save(str(image_path), "JPG")

            summary = con.build_task_summary(
                box_dir,
                {"5": [image_path.name]},
                [5],
                require_ocr=False,
            )
            row = con.TaskRowWidget(summary)
            texts = [label.text() for label in row.findChildren(QLabel)]

            self.assertEqual(summary["task_state"], "complete")
            self.assertNotIn("待核验", texts)
            self.assertFalse(any("待识别" in text for text in texts))
            row.close()

    def test_detail_scroll_viewport_has_an_opaque_light_background(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")

            viewport = window.scroll_area.viewport()
            self.assertIsInstance(viewport, con.SolidColorViewport)
            self.assertTrue(viewport.testAttribute(Qt.WA_OpaquePaintEvent))
            self.assertIsInstance(window.main_widget, con.SolidColorViewport)
            self.assertTrue(window.main_widget.testAttribute(Qt.WA_OpaquePaintEvent))
            self.assertEqual(viewport.palette().color(con.QPalette.Window), QColor(con.Theme.BG_MAIN))
            self.assertEqual(viewport.palette().color(con.QPalette.Base), QColor(con.Theme.BG_MAIN))
            window.close()

    def test_detail_cards_distinguish_required_and_optional_categories(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            con.write_meta(root / con.META_NAME, {"__requirements__": [1, 2, 3, 4]})
            window = con.BoxDetailWindow(root, "LYGU3043781")

            self.assertTrue(window.cards[1].is_required)
            self.assertEqual(window.cards[1].req_badge.text(), "必填")
            self.assertFalse(window.cards[5].is_required)
            self.assertEqual(window.cards[5].req_badge.text(), "选填")
            self.assertNotIn("line-through", window.cards[5].lbl_title.styleSheet())
            window.close()

    def test_unregistered_closed_door_photo_is_rendered_and_remains_deduplicated(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            source = root / "source.jpg"
            image = QImage(80, 60, QImage.Format_RGB32)
            image.fill(QColor("white"))
            self.assertTrue(image.save(str(source), "JPG"))
            recovered_name = f"{box_name}-5.jpg"
            (box_dir / recovered_name).write_bytes(source.read_bytes())
            con.write_meta(root / con.META_NAME, {box_name: {"5": []}})

            window = con.BoxDetailWindow(root, box_name)
            window.maybe_auto_ocr = lambda *_args: None

            self.assertEqual(window.meta["5"], [recovered_name])
            self.assertEqual(window.cards[5].lw.count(), 1)
            self.assertEqual(con.read_meta(root / con.META_NAME)[box_name]["5"], [recovered_name])

            with patch.object(window, "_use_background_image_import", return_value=False):
                window.add_files(5, [str(source)])

            self.assertEqual(window.meta["5"], [recovered_name])
            self.assertEqual(window.cards[5].lw.count(), 1)
            self.assertEqual(len(list(box_dir.glob("*.jpg"))), 1)
            window.close()

    def test_duplicate_image_is_skipped_across_categories(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))

            window.add_image_data(4, image)
            window.add_image_data(5, image)

            self.assertEqual(len(window.meta["4"]), 1)
            self.assertEqual(window.meta["5"], [])
            window.close()

    def test_local_file_duplicate_is_skipped_across_categories(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))
            image.save(str(source), "PNG")
            work = root / "work"
            work.mkdir()
            window = con.BoxDetailWindow(work, "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None

            window.add_files(4, [str(source)])
            window.add_files(5, [str(source)])

            self.assertEqual(len(window.meta["4"]), 1)
            self.assertEqual(window.meta["5"], [])
            window.close()

    def test_telegram_virtual_file_duplicate_is_skipped_across_categories(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))
            payload = window._qimage_png_bytes(image)

            window.add_virtual_files(4, [("telegram.png", payload)])
            window.add_virtual_files(5, [("telegram-copy.png", payload)])

            self.assertEqual(len(window.meta["4"]), 1)
            self.assertEqual(window.meta["5"], [])
            window.close()

    def test_telegram_virtual_image_is_saved_as_compact_jpeg(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            fixture = Path(__file__).parent / "fixtures" / "container_sample.jpg"
            image = QImage(str(fixture))
            png_payload = window._qimage_png_bytes(image)

            window.add_virtual_files(1, [("telegram.png", png_payload)])

            saved_name = window.meta["1"][0]
            saved_path = window.box_dir / saved_name
            self.assertEqual(saved_path.suffix.lower(), ".jpg")
            self.assertLess(saved_path.stat().st_size, len(png_payload) * 0.5)
            window.close()
            con.QThreadPool.globalInstance().waitForDone(5000)
            QApplication.processEvents()

    def test_failed_metadata_write_rolls_back_new_image(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))

            with patch.object(con, "update_meta", side_effect=OSError("disk full")), \
                    patch.object(con, "show_operation_error") as error_dialog:
                window.add_image_data(1, image)

            self.assertEqual(window.meta["1"], [])
            self.assertEqual(list(window.box_dir.glob("*.png")), [])
            self.assertEqual(error_dialog.call_args.args[1], "任务数据保存失败")
            window.close()

    def test_detail_ignores_photo_reference_outside_task_directory(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            outside = root / "outside.jpg"
            outside.write_bytes(b"outside")
            con.write_meta(root / con.META_NAME, {
                box_name: {
                    "1": ["../outside.jpg"],
                },
            })

            window = con.BoxDetailWindow(root, box_name)

            self.assertEqual(window.meta["1"], [])
            self.assertTrue(outside.is_file())
            window.close()

    def test_paste_uses_selected_category_and_advances_to_next_missing(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(24, 24, QImage.Format_RGB32)
            image.fill(QColor("white"))
            QApplication.clipboard().setImage(image)
            window.select_category(1)

            window.paste_to_selected_category()

            self.assertEqual(len(window.meta["1"]), 1)
            self.assertEqual(window.selected_category, 2)
            window.close()

    def test_ctrl_v_works_when_focus_is_outside_the_embedded_detail_page(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1, 2],
                box_name: {"1": [], "2": []},
            })
            with patch.object(con, "CONFIG_PATH", root / "config.json"):
                host = con.ContainerApp()
                host.root = root
                host.refresh()
                host.show()
                host.open_box_name(box_name)
                QApplication.processEvents()

                detail = host._detail_page
                detail.maybe_auto_ocr = lambda *_args: None
                image = QImage(80, 60, QImage.Format_RGB32)
                image.fill(QColor("white"))
                QApplication.clipboard().setImage(image)
                detail.select_category(1)
                host.btn_tasks.setFocus()
                QApplication.processEvents()

                QTest.keyClick(host.btn_tasks, Qt.Key_V, Qt.ControlModifier)
                QApplication.processEvents()

                self.assertEqual(len(detail.meta["1"]), 1)
                host.close()

    def test_delete_selects_its_category_and_allows_the_same_image_to_be_pasted_back(self):
        class FakeAction:
            def __init__(self, text):
                self.text = text

            def setIcon(self, _icon):
                pass

        class DeleteMenu:
            def __init__(self):
                self.actions = []

            def setStyleSheet(self, _style):
                pass

            def addAction(self, text):
                action = FakeAction(text)
                self.actions.append(action)
                return action

            def addSeparator(self):
                pass

            def exec(self, _position):
                return next(action for action in self.actions if action.text == "删除图片")

        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(80, 60, QImage.Format_RGB32)
            image.fill(QColor("white"))
            QApplication.clipboard().setImage(image)
            window.add_image_data(1, image)
            window.select_category(2)
            card = window.cards[1]
            item = card.lw.item(0)
            point = card.lw.visualItemRect(item).center()

            with patch.object(con, "QMenu", DeleteMenu):
                window.ctx(point, card.lw, 1)

            self.assertEqual(window.meta["1"], [])
            self.assertEqual(window.selected_category, 1)
            window.paste_to_selected_category()
            self.assertEqual(len(window.meta["1"]), 1)
            self.assertEqual(window.meta["2"], [])
            window.close()

    def test_repeated_pastes_reuse_the_task_image_hash_index(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            first = QImage(80, 60, QImage.Format_RGB32)
            first.fill(QColor("white"))
            second = QImage(80, 60, QImage.Format_RGB32)
            second.fill(QColor("black"))

            with patch.object(
                con,
                "build_image_hash_index",
                wraps=con.build_image_hash_index,
            ) as build_index:
                window.add_image_data(1, first)
                window.add_image_data(2, second)

            self.assertEqual(build_index.call_count, 1)
            window.close()

    def test_cross_category_duplicate_can_be_moved_to_the_selected_category(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.maybe_auto_ocr = lambda *_args: None
            image = QImage(80, 60, QImage.Format_RGB32)
            image.fill(QColor("white"))
            window.add_image_data(3, image)
            original_name = window.meta["3"][0]
            notices = []
            window._notify = lambda text, action_text="", callback=None: notices.append(
                (text, action_text, callback)
            )

            window.select_category(8)
            window.add_image_data(8, image)

            self.assertEqual(notices[-1][1], "移到这里")
            self.assertIsNotNone(notices[-1][2])
            notices[-1][2]()
            self.assertEqual(window.meta["3"], [])
            self.assertEqual(window.meta["8"], [original_name])
            self.assertEqual(len(list(window.box_dir.glob("*.jpg"))), 1)
            window.close()


class DashboardWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._config_dir = tempfile.TemporaryDirectory()
        self._config_patch = patch.object(
            con,
            "CONFIG_PATH",
            Path(self._config_dir.name) / "config.json",
        )
        self._config_patch.start()

    def tearDown(self):
        self._config_patch.stop()
        self._config_dir.cleanup()

    def test_saved_missing_work_root_has_explicit_recovery_state(self):
        config_path = Path(self._config_dir.name) / "config.json"
        missing_root = Path(self._config_dir.name) / "missing-work-root"
        fallback_root = Path(self._config_dir.name) / "available-work-root"
        fallback_root.mkdir()
        config_path.write_text(json.dumps({
            "root": str(missing_root),
            "hist": [str(missing_root), str(fallback_root)],
        }), encoding="utf-8")

        window = con.ContainerApp()

        self.assertEqual(window._work_root_state, "unavailable")
        self.assertFalse(window.directory_alert.isHidden())
        self.assertIn("不可用", window.directory_alert_title.text())
        self.assertEqual(window.directory_alert_path.toolTip(), str(missing_root))
        self.assertEqual(window.empty_action.text(), "重新选择目录")
        self.assertEqual(window.conf["root"], str(missing_root))
        self.assertIn(str(fallback_root), window.conf["hist"])
        window._rebuild_directory_history_menu()
        fallback_action = next(
            action for action in window.directory_history_menu.actions()
            if action.toolTip() == str(fallback_root)
        )
        fallback_action.trigger()
        self.assertEqual(window.root, fallback_root)
        self.assertEqual(window._work_root_state, "ready")
        window.close()

    def test_work_root_states_distinguish_unselected_missing_and_empty(self):
        window = con.ContainerApp()
        window.save_conf = lambda: True

        window.root = None
        window.refresh()
        self.assertEqual(window._work_root_state, "unselected")
        self.assertTrue(window.directory_alert.isHidden())
        self.assertEqual(window.empty_action.text(), "选择工作目录")

        missing_root = Path(self._config_dir.name) / "missing"
        window.root = missing_root
        window.refresh()
        self.assertEqual(window._work_root_state, "unavailable")
        self.assertFalse(window.directory_alert.isHidden())
        self.assertEqual(window.empty_action.text(), "重新选择目录")

        missing_root.mkdir()
        window.refresh()
        self.assertEqual(window._work_root_state, "ready")
        self.assertTrue(window.directory_alert.isHidden())

        empty_root = Path(self._config_dir.name) / "empty"
        empty_root.mkdir()
        window.root = empty_root
        window.refresh()
        self.assertEqual(window._work_root_state, "ready")
        self.assertTrue(window.directory_alert.isHidden())
        self.assertEqual(window.empty_action.text(), "批量新建")
        window.close()

    def test_remote_work_root_probe_does_not_block_window_startup(self):
        config_path = Path(self._config_dir.name) / "config.json"
        remote_root = Path(self._config_dir.name) / "simulated-remote-root"
        config_path.write_text(json.dumps({
            "root": str(remote_root),
            "hist": [str(remote_root)],
        }), encoding="utf-8")
        release_probe = threading.Event()
        probe_started = threading.Event()

        def delayed_probe(_path):
            probe_started.set()
            release_probe.wait(2)
            return False, "连接超时"

        with (
            patch.object(con, "is_remote_work_root", return_value=True),
            patch.object(con, "probe_work_root", side_effect=delayed_probe),
        ):
            started_at = time.perf_counter()
            window = con.ContainerApp()
            startup_elapsed = time.perf_counter() - started_at
            self.assertTrue(probe_started.wait(0.5))
            self.assertLess(startup_elapsed, 0.5)
            self.assertEqual(window._work_root_state, "checking")

            release_probe.set()
            deadline = time.perf_counter() + 1
            while time.perf_counter() < deadline and window._work_root_state == "checking":
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(window._work_root_state, "unavailable")
            window.close()

    def test_dashboard_filters_anomalies_pending_and_completed_tasks(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            complete = "MSCU6639870"
            pending = "LYGU3043781"
            anomaly = "错误箱号"
            for name in (complete, pending, anomaly):
                (root / name).mkdir()
            (root / ".photocollect").mkdir()
            image = QImage(12, 12, QImage.Format_RGB32)
            image.fill(QColor("white"))
            image.save(str(root / complete / "done.png"), "PNG")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                complete: {"1": ["done.png"]},
                pending: {"1": []},
                anomaly: {"1": ["missing-on-disk.png"]},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: None
            window.root = root
            window.refresh()

            states = {item["name"]: item["task_state"] for item in window.task_data}
            self.assertNotIn(".photocollect", states)
            self.assertEqual(states[complete], "complete")
            self.assertEqual(states[pending], "pending")
            self.assertEqual(states[anomaly], "anomaly")

            window.set_filter("anomaly")
            self.assertEqual(window.list.count(), 1)
            self.assertEqual(window.list.item(0).data(Qt.UserRole), anomaly)
            window.close()

    def test_default_status_sort_uses_state_then_container_number(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            anomaly = "错误箱号"
            pending_first = "LYGU3043781"
            prefix = "TGHU123456"
            pending_second = prefix + str(con.iso6346_check_digit(prefix))
            complete = "MSCU6639870"
            for name in (anomaly, pending_first, pending_second, complete):
                (root / name).mkdir()

            image = QImage(12, 12, QImage.Format_RGB32)
            image.fill(QColor("white"))
            image.save(str(root / pending_second / "one.png"), "PNG")
            image.save(str(root / complete / "one.png"), "PNG")
            image.save(str(root / complete / "two.png"), "PNG")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1, 2],
                anomaly: {"1": [], "2": []},
                pending_first: {"1": [], "2": []},
                pending_second: {"1": ["one.png"], "2": []},
                complete: {"1": ["one.png"], "2": ["two.png"]},
            })

            window = con.ContainerApp()
            window.save_conf = lambda: None
            window.root = root
            window.set_sort("status")
            window.refresh()

            rendered = [window.list.item(i).data(Qt.UserRole) for i in range(window.list.count())]
            self.assertEqual(rendered, [anomaly, pending_first, pending_second, complete])
            window.close()

    def test_sidebar_and_detail_actions_remain_compact_at_narrow_widths(self):
        with qt_temporary_directory() as tmp:
            detail = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            detail.resize(700, 700)
            detail.apply_responsive_layout()
            self.assertEqual(detail.btn_next_missing.text(), "")
            self.assertEqual(detail.btn_next_missing.width(), 34)
            detail.resize(1000, 700)
            detail.apply_responsive_layout()
            self.assertEqual(detail.btn_next_missing.text(), "下一缺失项")
            detail.close()

            window = con.ContainerApp()
            window.save_conf = lambda: True
            window._sidebar_user_collapsed = True
            window._sidebar_auto_collapsed = False
            window._apply_sidebar_state()
            self.assertTrue(all(button.width() == 32 for button in window._sidebar_buttons))
            window._sidebar_user_collapsed = False
            window._apply_sidebar_state()
            self.assertTrue(all(button.maximumWidth() > 32 for button in window._sidebar_buttons))
            window.close()

    def test_embedded_detail_recalculates_cards_after_main_layout(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {"__requirements__": [1, 2, 3, 4]})
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.resize(1280, 850)
            window.show()
            window.open_box_name(box_name)
            QApplication.processEvents()

            detail = window._detail_page
            self.assertGreaterEqual(detail.cards[1].width(), 230)
            self.assertEqual(detail.cards[1].y(), detail.cards[4].y())
            self.assertGreater(detail.cards[5].y(), detail.cards[1].y())
            card_rects = [detail.cards[idx].geometry() for idx in range(1, 9)]
            for position, rect in enumerate(card_rects):
                self.assertGreaterEqual(rect.left(), 0)
                self.assertLessEqual(rect.right(), detail.main_widget.rect().right())
                for other in card_rects[position + 1:]:
                    self.assertFalse(rect.intersects(other))
            window.close()

    def test_fifth_and_eighth_cards_can_be_clicked_and_receive_pastes(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": list(range(1, 9)),
                box_name: {str(idx): [] for idx in range(1, 9)},
            })
            window = con.ContainerApp()
            window.root = root
            window.resize(1280, 850)
            window.show()
            window.open_box_name(box_name)
            QApplication.processEvents()
            detail = window._detail_page
            detail.maybe_auto_ocr = lambda *_args: None

            for idx, color in ((5, "red"), (8, "blue")):
                card = detail.cards[idx]
                QTest.mouseClick(
                    card.lw.viewport(),
                    Qt.LeftButton,
                    pos=card.lw.viewport().rect().center(),
                )
                self.assertEqual(detail.selected_category, idx)
                image = QImage(80, 60, QImage.Format_RGB32)
                image.fill(QColor(color))
                QApplication.clipboard().setImage(image)
                QTest.keyClick(card.lw.viewport(), Qt.Key_V, Qt.ControlModifier)
                QApplication.processEvents()
                self.assertEqual(len(detail.meta[str(idx)]), 1)
            window.close()

    def test_configuration_write_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            con, "CONFIG_PATH", Path(tmp) / "config" / "config.json"
        ):
            window = con.ContainerApp()
            with patch.object(con.os, "replace", side_effect=OSError("read only")):
                self.assertFalse(window.save_conf())
            window.close()

    def test_auto_ocr_switch_is_saved_and_updates_open_detail(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp) / "photos"
            box_name = "LYGU3043781"
            (root / box_name).mkdir(parents=True)
            con.write_meta(root / con.META_NAME, {box_name: {}})
            window = con.ContainerApp()
            window.root = root
            window.open_box_name(box_name)
            window.show()
            QApplication.processEvents()

            self.assertTrue(window.chk_auto_ocr.isChecked())
            window._detail_page._pending_auto_ocr = (5, "queued.jpg", False)
            QTest.mouseClick(window.chk_auto_ocr, Qt.LeftButton)
            QApplication.processEvents()

            saved = con.json.loads(con.CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertFalse(saved["auto_ocr_enabled"])
            self.assertEqual(window._detail_page.lbl_ocr_status.text(), "OCR 自动关闭")
            self.assertIsNone(window._detail_page._pending_auto_ocr)
            window.close()

    def test_frameless_window_controls_follow_real_click_and_double_click_paths(self):
        window = con.ContainerApp()
        window.show()
        QApplication.processEvents()

        QTest.mouseClick(window.btn_maximize, Qt.LeftButton)
        QApplication.processEvents()
        self.assertTrue(window.isMaximized())

        QTest.mouseDClick(window.topbar, Qt.LeftButton, pos=window.topbar.rect().center())
        QApplication.processEvents()
        self.assertFalse(window.isMaximized())

        QTest.mouseClick(window.btn_minimize, Qt.LeftButton)
        QApplication.processEvents()
        self.assertTrue(bool(window.windowState() & Qt.WindowMinimized))
        window.showNormal()
        QApplication.processEvents()

        QTest.mouseClick(window.btn_close, Qt.LeftButton)
        QApplication.processEvents()
        self.assertFalse(window.isVisible())

    def test_maximized_window_restores_when_titlebar_drag_starts(self):
        window = con.ContainerApp()
        window.resize(1200, 760)
        window.show()
        QApplication.processEvents()
        expected_normal_size = window.size()
        window.showMaximized()
        QApplication.processEvents()
        self.assertTrue(window.isMaximized())

        start = window.topbar.rect().center()
        window._drag_press_global = window.topbar.mapToGlobal(start)
        window._drag_restore_ratio = start.x() / window.topbar.width()
        moved_global = window._drag_press_global + QPoint(80, 120)
        restored = window._restore_from_maximized_drag(
            moved_global,
            QPoint(start.x() + 80, start.y() + 120),
        )
        QApplication.processEvents()

        self.assertTrue(restored)
        self.assertFalse(window.isMaximized())
        self.assertEqual(window.size(), expected_normal_size)
        window.close()

    def test_frameless_window_exposes_windows_snap_contract(self):
        window = con.ContainerApp()
        flags = window.windowFlags()

        self.assertTrue(flags & Qt.WindowSystemMenuHint)
        self.assertTrue(flags & Qt.WindowMinimizeButtonHint)
        self.assertTrue(flags & Qt.WindowMaximizeButtonHint)
        self.assertLessEqual(window.minimumWidth(), 500)

        window.show()
        QApplication.processEvents()
        maximize_center = window.btn_maximize.mapToGlobal(window.btn_maximize.rect().center())
        self.assertEqual(window._windows_hit_test(maximize_center), con.HTMAXBUTTON)
        caption_point = window.topbar.mapToGlobal(window.topbar.rect().center())
        self.assertEqual(window._windows_hit_test(caption_point), con.HTCAPTION)
        close_center = window.btn_close.mapToGlobal(window.btn_close.rect().center())
        self.assertIsNone(window._windows_hit_test(close_center))
        window.close()

    def test_native_snap_hit_keeps_custom_maximize_button_clickable(self):
        window = con.ContainerApp()
        window.show()
        QApplication.processEvents()
        maximize_center = window.btn_maximize.mapToGlobal(window.btn_maximize.rect().center())

        self.assertTrue(
            window._handle_windows_maximize_mouse(con.WM_LBUTTONDOWN, 0, maximize_center)
        )
        self.assertTrue(window.btn_maximize.isDown())
        self.assertTrue(
            window._handle_windows_maximize_mouse(con.WM_LBUTTONUP, 0, maximize_center)
        )
        QApplication.processEvents()

        self.assertTrue(window.isMaximized())
        self.assertFalse(window.btn_maximize.isDown())
        window.close()

    def test_command_bar_reflows_without_overlap_in_narrow_snap_zone(self):
        window = con.ContainerApp()
        window.resize(455, 700)
        window.show()
        QApplication.processEvents()

        search_rect = window.search.geometry()
        actions_rect = window.command_actions.geometry()
        self.assertTrue(window._command_bar_compact)
        self.assertGreater(actions_rect.top(), search_rect.bottom())
        self.assertFalse(search_rect.intersects(actions_rect))

        window.resize(640, 700)
        QApplication.processEvents()
        self.assertFalse(window._command_bar_compact)
        self.assertEqual(
            window.search.geometry().center().y(),
            window.command_actions.geometry().center().y(),
        )
        window.close()

    def test_command_buttons_and_menus_share_one_visual_contract(self):
        window = con.ContainerApp()
        window.show()
        QApplication.processEvents()

        for button in (
            window.btn_new,
            window.btn_sort,
            window.btn_refresh,
            window.btn_export,
            window.report_page.btn_refresh,
            window.report_page.btn_export,
            window.btn_directory_history,
            window.btn_reselect_directory,
        ):
            self.assertEqual(button.height(), 34)
        for button in (
            window.btn_sort,
            window.btn_refresh,
            window.btn_export,
            window.report_page.btn_refresh,
            window.btn_directory_history,
        ):
            self.assertEqual(button.iconSize(), QSize(16, 16))

        for menu in (
            window.btn_sort.menu(),
            window.btn_export.menu(),
            window.btn_directory_history.menu(),
        ):
            self.assertIn("border-radius: 7px", menu.styleSheet())
            self.assertIn("padding: 6px 24px 6px 10px", menu.styleSheet())
        zip_actions = window.btn_export.menu().actions()[:2]
        self.assertTrue(all(not action.icon().isNull() for action in zip_actions))

        with qt_temporary_directory() as tmp:
            detail = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            self.assertEqual(detail.btn_next_missing.height(), 34)
            self.assertEqual(detail.btn_ocr.iconSize(), QSize(16, 16))
            self.assertIn("border-radius: 7px", detail.btn_more.menu().styleSheet())
            detail.close()
        window.close()

    def test_status_text_and_report_layout_remain_readable_at_snap_widths(self):
        self.assertEqual(con.status_badge("待核验", "warn", compact=True).height(), 24)
        self.assertEqual(con.status_badge("任务箱号异常", "bad").height(), 28)
        self.assertEqual(
            con.task_issue_reason({
                "box_number_valid": True,
                "ocr_status": "mismatch",
                "ocr_container": "MSCU6639870",
            }),
            "OCR识别为 MSCU6639870，与任务箱号不一致",
        )
        self.assertEqual(
            con.task_issue_reason({
                "box_number_valid": True,
                "ocr_status": "error",
            }),
            "OCR识别失败，请打开任务重试",
        )

        row = con.TaskRowWidget({
            "name": "LYGU3043781",
            "ocr_size_type": "22G1",
            "ocr_status": "mismatch",
            "ocr_container": "MSCU6639870",
            "missing": ["全开门空箱", "全开门1/2箱", "全开门满箱", "半关门满箱"],
            "pic_cnt": 2,
            "task_state": "anomaly",
            "box_number_valid": True,
            "ocr_enabled": True,
            "has_auto_ocr_photo": True,
        })
        row.resize(330, 64)
        row.show()
        QApplication.processEvents()
        row_labels = row.findChildren(con.ElidedLabel)
        self.assertEqual(len(row_labels), 2)
        self.assertIs(row_labels[0], row.title_label)
        self.assertIs(row_labels[1], row.detail_label)
        self.assertEqual(row.title_label.fullText(), "LYGU3043781  ·  22G1")
        self.assertGreater(row.title_label.width(), 0)
        self.assertTrue(row.title_label.text())
        self.assertIn("OCR识别为 MSCU6639870", row.detail_label.fullText())
        self.assertTrue(row.detail_label.toolTip())
        for width in (641, 1000):
            row.resize(width, 64)
            QApplication.processEvents()
            self.assertGreater(row.title_label.width(), 0)
            self.assertIn("LYGU3043781", row.title_label.text())
        row.close()

        window = con.ContainerApp()
        window.resize(640, 700)
        window.show()
        QApplication.processEvents()
        self.assertTrue(window._dashboard_summary_compact)
        self.assertGreater(
            window.dashboard_stats_group.geometry().top(),
            window.dashboard_title_group.geometry().bottom(),
        )

        window.show_report()
        QApplication.processEvents()
        report = window.report_page
        self.assertTrue(report._responsive_compact)
        self.assertGreater(
            report.details_controls.geometry().top(),
            report.details_title_group.geometry().bottom(),
        )
        self.assertTrue(report.category_table.isColumnHidden(1))
        self.assertTrue(report.details_table.isColumnHidden(6))

        window.resize(1024, 700)
        QApplication.processEvents()
        self.assertFalse(window._dashboard_summary_compact)
        self.assertFalse(report._responsive_compact)
        self.assertFalse(report.category_table.isColumnHidden(1))
        self.assertFalse(report.details_table.isColumnHidden(6))
        window.close()

    def test_failure_details_can_be_copied_and_logs_opened(self):
        dialog = con.OperationErrorDialog(
            "ZIP 导出失败",
            "交接包没有生成，已有文件保持不变。",
            "PermissionError: access denied",
        )
        dialog.copy_details()
        copied = self.app.clipboard().text()
        self.assertIn("ZIP 导出失败", copied)
        self.assertIn("已有文件保持不变", copied)
        self.assertIn("PermissionError: access denied", copied)

        with patch.object(con.QDesktopServices, "openUrl", return_value=True) as open_url:
            dialog.btn_logs.click()
        open_url.assert_called_once()
        self.assertTrue(open_url.call_args.args[0].isLocalFile())
        dialog.close()

        with patch.object(con.QTimer, "singleShot"):
            health = con.DataHealthDialog(Path(self._config_dir.name))
        health.on_error("metadata could not be parsed")
        self.assertTrue(health.btn_copy_health.isEnabled())
        health.copy_report()
        self.assertIn("metadata could not be parsed", self.app.clipboard().text())
        health.close()

    def test_keyboard_order_and_icon_tooltips_cover_primary_workflows(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1, 2],
                box_name: {"1": [], "2": []},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()
            window.show()
            QApplication.processEvents()

            expected_dashboard_order = (
                window.btn_new,
                window.search,
                window.btn_sort,
                window.btn_refresh,
                window.btn_export,
                window.filter_buttons["all"],
                window.list,
            )
            for current, expected_next in zip(
                expected_dashboard_order,
                expected_dashboard_order[1:],
            ):
                current.setFocus()
                QTest.keyClick(current, Qt.Key_Tab)
                QApplication.processEvents()
                self.assertIs(QApplication.focusWidget(), expected_next)
            window.filter_buttons["all"].setFocus()
            QTest.keyClick(window.filter_buttons["all"], Qt.Key_Right)
            QApplication.processEvents()
            self.assertIs(QApplication.focusWidget(), window.filter_buttons["pending"])

            window.open_box_name(box_name)
            detail = window._detail_page
            expected_detail_order = (
                detail.btn_back,
                detail.btn_next_missing,
                detail.btn_ocr,
                detail.btn_more,
                # The auto-sort button is only focusable when the git-ignored model is installed.
                *([detail.auto_sort_zone.btn_choose] if detail.auto_sort_zone.btn_choose.isEnabled() else []),
                detail.cards[1],
                detail.cards[1].btn_add,
                detail.cards[1].lw,
                detail.cards[2],
            )
            for current, expected_next in zip(
                expected_detail_order,
                expected_detail_order[1:],
            ):
                current.setFocus()
                QTest.keyClick(current, Qt.Key_Tab)
                QApplication.processEvents()
                self.assertIs(QApplication.focusWidget(), expected_next)

            icon_only_buttons = [
                button
                for button in window.findChildren(QAbstractButton)
                if not button.icon().isNull() and not button.text().strip()
            ]
            self.assertGreaterEqual(len(icon_only_buttons), 7)
            self.assertEqual(
                [button for button in icon_only_buttons if not button.toolTip().strip()],
                [],
            )
            window.close()

    def test_primary_actions_fit_reference_and_snap_viewports(self):
        def assert_inside(widget, ancestor):
            top_left = widget.mapTo(ancestor, QPoint(0, 0))
            bottom_right = top_left + QPoint(
                max(0, widget.width() - 1),
                max(0, widget.height() - 1),
            )
            self.assertTrue(ancestor.rect().contains(top_left))
            self.assertTrue(ancestor.rect().contains(bottom_right))

        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": list(range(1, 9)),
                box_name: {str(index): [] for index in range(1, 9)},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()
            window.show()

            for width, height in (
                (455, 700),
                (640, 700),
                (1024, 700),
                (1366, 768),
                (1920, 1080),
            ):
                window.resize(width, height)
                window.show_dashboard()
                QApplication.processEvents()
                for widget in (
                    window.btn_new,
                    window.search,
                    window.command_actions,
                    window.filter_buttons["all"],
                    window.list_stack,
                ):
                    self.assertTrue(widget.isVisible())
                    assert_inside(widget, window)

                window.show_report()
                QApplication.processEvents()
                for widget in (
                    window.report_page.btn_refresh,
                    window.report_page.btn_export,
                    window.report_page.details_controls,
                    window.report_page.details_table,
                ):
                    self.assertTrue(widget.isVisible())
                    assert_inside(widget, window)

                window.open_box_name(box_name)
                QApplication.processEvents()
                detail = window._detail_page
                for widget in (
                    detail.btn_back,
                    detail.lbl_name,
                    detail.btn_next_missing,
                    detail.btn_ocr,
                    detail.btn_more,
                    detail.scroll_area,
                ):
                    self.assertTrue(widget.isVisible())
                    assert_inside(widget, window)
                window.on_back_to_dashboard()
                QApplication.processEvents()
            window.close()

    def test_report_detail_navigation_returns_to_report_page(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            image = QImage(12, 12, QImage.Format_RGB32)
            image.fill(QColor("white"))
            image.save(str(box_dir / "one.png"), "PNG")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1, 2],
                box_name: {"1": ["one.png"], "2": []},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()

            window.show_report()
            self.assertIs(window.stack.currentWidget(), window.report_page)
            self.assertEqual(window.collection_report.total_tasks, 1)
            self.assertEqual(window.report_page.category_table.rowCount(), 8)
            self.assertEqual(window.stack.count(), 2)

            window.open_box_name(box_name)
            self.assertIsInstance(window.stack.currentWidget(), con.BoxDetailWindow)
            self.assertEqual(window.stack.count(), 3)
            window.on_back_to_dashboard()
            self.assertIs(window.stack.currentWidget(), window.report_page)
            self.assertEqual(window.stack.count(), 2)
            window.close()

    def test_two_action_toast_fits_narrow_window(self):
        window = con.ContainerApp()
        window.resize(400, 700)
        window.show()
        self.app.processEvents()

        window.show_toast(
            "采集看板已经导出，可以打开文件或查看所在文件夹。",
            "打开文件",
            lambda: None,
            8000,
            "打开文件夹",
            lambda: None,
        )
        self.app.processEvents()

        self.assertGreaterEqual(window.toast.x(), 0)
        self.assertLessEqual(window.toast.x() + window.toast.width(), window.width())
        window.close()

    def test_data_health_dialog_runs_read_only_scan_and_copies_result(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                box_name: {"1": ["door.jpg"]},
            })
            dialog = con.DataHealthDialog(root)
            dialog.show()
            deadline = time.perf_counter() + 2
            while time.perf_counter() < deadline and dialog.report is None:
                self.app.processEvents()
                time.sleep(0.01)

            self.assertIsNotNone(dialog.report)
            self.assertEqual(dialog.report.issues, ())
            self.assertTrue(dialog.btn_copy_health.isEnabled())
            dialog.copy_report()
            self.assertIn("未发现问题", self.app.clipboard().text())
            dialog.close()

    def test_data_health_scan_runs_outside_ui_thread(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            worker_threads = []

            def fake_scan(scan_root, **_kwargs):
                worker_threads.append(threading.get_ident())
                return con.HealthReport(
                    root=Path(scan_root),
                    checked_at="2026-07-26T12:00:00",
                    duration_ms=1,
                    task_count=0,
                    referenced_photo_count=0,
                    internal_directory_count=0,
                    used_backup=False,
                    issues=(),
                )

            with patch.object(con, "scan_data_health", side_effect=fake_scan):
                dialog = con.DataHealthDialog(root)
                dialog.show()
                deadline = time.perf_counter() + 2
                while time.perf_counter() < deadline and dialog.report is None:
                    self.app.processEvents()
                    time.sleep(0.01)

            self.assertIsNotNone(dialog.report)
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            dialog.close()

    def test_data_health_dialog_shows_phase_and_can_cancel(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            worker_started = threading.Event()

            def fake_scan(
                _scan_root,
                *,
                cancel_event=None,
                progress_callback=None,
                **_kwargs,
            ):
                progress_callback(1, 4, "检查元数据")
                worker_started.set()
                while not cancel_event.wait(0.01):
                    pass
                raise con.HealthScanCancelled()

            with patch.object(con, "scan_data_health", side_effect=fake_scan):
                dialog = con.DataHealthDialog(root)
                dialog.show()
                deadline = time.perf_counter() + 2
                while (
                    time.perf_counter() < deadline
                    and (
                        not worker_started.is_set()
                        or "检查元数据" not in dialog.summary.text()
                    )
                ):
                    self.app.processEvents()
                    time.sleep(0.01)

                started = worker_started.is_set()
                phase_text = dialog.summary.text()
                cancel_enabled = dialog.btn_cancel_health.isEnabled()
                dialog.cancel_check()
                deadline = time.perf_counter() + 2
                while time.perf_counter() < deadline and dialog._task is not None:
                    self.app.processEvents()
                    time.sleep(0.01)

            self.assertTrue(started)
            self.assertIn("检查元数据", phase_text)
            self.assertTrue(cancel_enabled)
            self.assertIsNone(dialog._task)
            self.assertIn("已取消", dialog.summary.text())
            self.assertFalse(dialog.btn_cancel_health.isEnabled())
            dialog.close()

    def test_return_without_changes_does_not_rebuild_dashboard_or_report(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                box_name: {"1": []},
            })
            window = con.ContainerApp()
            window.root = root
            window.refresh()
            window.open_box_name(box_name)

            with patch.object(window, "refresh") as full_refresh, patch.object(
                window, "_refresh_collection_report"
            ) as report_refresh:
                window.on_back_to_dashboard()
                QApplication.processEvents()

            full_refresh.assert_not_called()
            report_refresh.assert_not_called()
            self.assertIs(window.stack.currentWidget(), window.dashboard_page)
            window.close()

    def test_detail_change_updates_one_task_without_full_list_rebuild(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            changed_box = "LYGU3043781"
            other_box = "MSCU6639870"
            for box_name in (changed_box, other_box):
                (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                changed_box: {"1": []},
                other_box: {"1": []},
            })
            window = con.ContainerApp()
            window.root = root
            window.refresh()

            with patch.object(window, "refresh") as full_refresh:
                window.open_box_name(changed_box)
                detail = window._detail_page
                image = QImage(24, 24, QImage.Format_RGB32)
                image.fill(QColor("white"))
                image.save(str(detail.box_dir / "one.jpg"), "JPG")
                detail.meta["1"] = ["one.jpg"]
                self.assertTrue(detail.save_meta())
                window.on_back_to_dashboard()
                QApplication.processEvents()

            full_refresh.assert_not_called()
            summaries = {item["name"]: item for item in window.task_data}
            self.assertEqual(summaries[changed_box]["pic_cnt"], 1)
            self.assertEqual(summaries[changed_box]["task_state"], "complete")
            self.assertEqual(summaries[other_box]["pic_cnt"], 0)
            self.assertEqual(window.list.count(), 2)
            window.close()

    def test_dashboard_refresh_defers_report_render_until_report_is_opened(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                box_name: {"1": []},
            })
            window = con.ContainerApp()
            window.root = root

            with patch.object(window.report_page, "set_report", wraps=window.report_page.set_report) as render:
                window.refresh()
                self.assertEqual(render.call_count, 0)
                window.show_report()
                self.assertEqual(render.call_count, 1)

            window.close()

    def test_report_table_shows_uploaded_optional_photos_without_progress_ghosts(self):
        report = con.build_collection_report([{
            "name": "LYGU3043781",
            "task_state": "complete",
            "ocr_status": "match",
            "pic_cnt": 3,
            "required_categories": [1],
            "category_counts": {"1": 1, "6": 2},
            "last_updated": "2026-07-11 10:00:00",
        }], con.ALL_CATEGORIES)
        page = con.CollectionDashboardPage()

        page.set_report(report, "C:/photos")
        page.set_report(report, "C:/photos")
        page.show()
        QApplication.processEvents()

        visible_bars = [
            bar for bar in page.category_table.findChildren(QProgressBar)
            if bar.isVisible()
        ]
        self.assertEqual(page.category_table.columnCount(), 6)
        self.assertEqual(len(visible_bars), 8)
        self.assertEqual(page.category_table.item(5, 4).text(), "2")
        self.assertEqual(page.category_table.cellWidget(5, 5).format(), "非必填")
        page.close()

    def test_excel_report_export_does_not_change_collection_metadata(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                "__export_count__": 4,
                box_name: {"1": []},
            })
            destination = root / "report.xlsx"
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()
            notices = []
            window.show_toast = lambda *args: notices.append(args)

            with (
                patch.object(window, "_confirm_export_preflight", return_value=True),
                patch.object(
                    con.QFileDialog,
                    "getSaveFileName",
                    return_value=(str(destination), "Excel 工作簿 (*.xlsx)"),
                ),
            ):
                window.export_collection_excel()

            deadline = time.perf_counter() + 5
            while time.perf_counter() < deadline and window._export_task is not None:
                self.app.processEvents()
                time.sleep(0.01)

            self.assertIsNone(window._export_task)
            self.assertTrue(destination.is_file())
            self.assertEqual(con.read_meta(root / con.META_NAME)["__export_count__"], 4)
            self.assertEqual(notices[-1][1], "打开文件")
            self.assertEqual(notices[-1][4], "打开文件夹")
            window.close()

    def test_excel_export_runs_off_ui_thread_and_can_be_cancelled(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                box_name: {"1": []},
            })
            destination = root / "cancelled.xlsx"
            worker_threads = []
            worker_started = threading.Event()
            notices = []

            def fake_export(
                _report,
                _destination,
                _work_root,
                generated_at=None,
                cancel_event=None,
                progress_callback=None,
            ):
                worker_threads.append(threading.get_ident())
                progress_callback(1, 5, "生成采集概览")
                worker_started.set()
                while not cancel_event.wait(0.01):
                    pass
                raise con.ReportExportCancelled()

            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()
            window.show_toast = lambda *args: notices.append(args)

            with (
                patch.object(window, "_confirm_export_preflight", return_value=True),
                patch.object(
                    con.QFileDialog,
                    "getSaveFileName",
                    return_value=(str(destination), "Excel 工作簿 (*.xlsx)"),
                ),
                patch.object(con, "export_collection_report_xlsx", side_effect=fake_export),
            ):
                window.export_collection_excel()
                deadline = time.perf_counter() + 2
                while time.perf_counter() < deadline and not worker_started.is_set():
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(worker_started.is_set())
                self.assertFalse(window.btn_export.isEnabled())
                self.assertFalse(window.report_page.btn_export.isEnabled())
                window._export_task.cancel()
                deadline = time.perf_counter() + 2
                while time.perf_counter() < deadline and window._export_task is not None:
                    self.app.processEvents()
                    time.sleep(0.01)

            self.assertIsNone(window._export_task)
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            self.assertFalse(destination.exists())
            self.assertEqual(notices[-1][0], "已取消 Excel 导出")
            self.assertTrue(window.btn_export.isEnabled())
            window.close()

    def test_export_preflight_cancel_does_not_open_save_dialog(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "one.jpg").write_bytes(b"photo")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                box_name: {"1": ["one.jpg"]},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()

            with (
                patch.object(window, "_confirm_export_preflight", return_value=False),
                patch.object(con.QFileDialog, "getSaveFileName") as save_dialog,
            ):
                window.export_handoff("all")
                window.export_collection_excel()

            save_dialog.assert_not_called()
            self.assertEqual(list(root.glob("*.zip")), [])
            self.assertEqual(list(root.glob("*.xlsx")), [])
            window.close()

    def test_save_dialog_cancel_and_export_error_do_not_increment_export_count(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "one.jpg").write_bytes(b"photo")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1],
                "__export_count__": 6,
                box_name: {"1": ["one.jpg"]},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()

            with (
                patch.object(window, "_confirm_export_preflight", return_value=True),
                patch.object(con.QFileDialog, "getSaveFileName", return_value=("", "")),
            ):
                window.export_handoff("all")
                window.export_collection_excel()

            window._export_root = root
            with patch.object(con, "show_operation_error") as error_dialog:
                window.on_export_error("disk full")

            self.assertEqual(con.read_meta(root / con.META_NAME)["__export_count__"], 6)
            self.assertEqual(list(root.glob("*.zip")), [])
            self.assertEqual(list(root.glob("*.xlsx")), [])
            self.assertEqual(error_dialog.call_args.args[1], "ZIP 导出失败")
            window.close()

    def test_zip_preflight_uses_selected_scope_and_actual_source_size(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            complete = "MSCU6639870"
            pending = "LYGU3043781"
            for name in (complete, pending):
                (root / name).mkdir()
            (root / complete / "done-1.jpg").write_bytes(b"complete-one")
            (root / complete / "done-2.jpg").write_bytes(b"complete-two")
            (root / pending / "partial.jpg").write_bytes(b"pending")
            con.write_meta(root / con.META_NAME, {
                "__requirements__": [1, 2],
                complete: {"1": ["done-1.jpg"], "2": ["done-2.jpg"]},
                pending: {"1": ["partial.jpg"], "2": []},
            })
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = root
            window.refresh()
            captured = []
            window._confirm_export_preflight = lambda preflight: captured.append(preflight) or False

            window.export_handoff("all")
            window.export_handoff("complete")

            all_preflight, complete_preflight = captured
            self.assertEqual(all_preflight.kind, "zip")
            self.assertEqual(all_preflight.scope, "全部任务")
            self.assertEqual(all_preflight.task_count, 2)
            self.assertEqual(all_preflight.photo_count, 3)
            self.assertEqual(
                all_preflight.source_photo_bytes,
                len(b"complete-one") + len(b"complete-two") + len(b"pending"),
            )
            self.assertEqual(complete_preflight.scope, "仅已完成任务")
            self.assertEqual(complete_preflight.task_count, 1)
            self.assertEqual(complete_preflight.complete_count, 1)
            self.assertEqual(complete_preflight.photo_count, 2)
            menu_texts = [action.text() for action in window.btn_export.menu().actions()]
            self.assertIn("照片交接包 ZIP · 全部任务", menu_texts)
            self.assertIn("照片交接包 ZIP · 仅已完成", menu_texts)
            self.assertIn("采集看板 Excel", menu_texts)
            window.close()

    def test_zip_success_records_original_root_and_offers_open_folder(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            export_root = Path(first_tmp)
            switched_root = Path(second_tmp)
            con.write_meta(export_root / con.META_NAME, {"__export_count__": 2})
            con.write_meta(switched_root / con.META_NAME, {"__export_count__": 9})
            output = export_root / "handoff.zip"
            output.write_bytes(b"zip")
            window = con.ContainerApp()
            window.save_conf = lambda: True
            window.root = switched_root
            window._export_root = export_root
            notices = []
            window.show_toast = lambda *args: notices.append(args)

            window.on_export_finished(SimpleNamespace(
                output_path=output,
                task_count=1,
                photo_count=3,
                optimized_photo_count=0,
                source_photo_bytes=0,
                exported_photo_bytes=0,
            ))

            self.assertEqual(con.read_meta(export_root / con.META_NAME)["__export_count__"], 3)
            self.assertEqual(con.read_meta(switched_root / con.META_NAME)["__export_count__"], 9)
            self.assertEqual(notices[-1][1], "打开文件夹")
            self.assertTrue(callable(notices[-1][2]))
            window.close()


class ClaudeThemeInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_escape_returns_from_detail_to_previous_page(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            with patch.object(con, "CONFIG_PATH", root / "config.json"):
                window = con.ContainerApp()
                window.save_conf = lambda: True
                window.root = root
                window.refresh()
                window.show()
                window.open_box_name(box_name)
                QApplication.processEvents()
                self.assertIsNotNone(window._detail_page)
                self.assertIs(window.stack.currentWidget(), window._detail_page)

                QTest.keyClick(window, Qt.Key_Escape)
                QApplication.processEvents()

                self.assertIsNone(window._detail_page)
                self.assertIs(window.stack.currentWidget(), window.dashboard_page)
                window.close()

    def test_keyboard_focus_on_category_card_selects_it_for_paste(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QFocusEvent

        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            self.assertNotEqual(window.selected_category, 3)

            QApplication.sendEvent(
                window.cards[3],
                QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason),
            )
            QApplication.processEvents()

            self.assertEqual(window.selected_category, 3)
            self.assertTrue(window.cards[3].is_selected)
            window.close()

    def test_export_success_log_does_not_create_native_status_bar(self):
        from PySide6.QtWidgets import QStatusBar

        with qt_temporary_directory() as tmp:
            with patch.object(con, "CONFIG_PATH", Path(tmp) / "config.json"):
                window = con.ContainerApp()
                window.save_conf = lambda: True

                window.log("交接包导出成功")
                QApplication.processEvents()

                self.assertIsNone(window.findChild(QStatusBar))
                window.close()

    def test_ocr_callback_after_page_disposal_is_ignored(self):
        with qt_temporary_directory() as tmp:
            window = con.BoxDetailWindow(Path(tmp), "LYGU3043781")
            window.prepare_for_disposal()

            window.on_ocr_finished(
                {"container_no": "LYGU3043781", "check_digit_valid": True},
                "closed.jpg",
                automatic=True,
            )
            window.on_ocr_error("engine exploded", "closed.jpg", automatic=True)

            self.assertEqual(window.ocr_result, {})
            window.close()

    def test_reload_from_disk_picks_up_new_global_requirements(self):
        with qt_temporary_directory() as tmp:
            root = Path(tmp)
            (root / "LYGU3043781").mkdir()
            con.write_meta(root / con.META_NAME, {"__requirements__": [1, 2]})
            window = con.BoxDetailWindow(root, "LYGU3043781")
            self.assertEqual(list(window.get_effective_reqs()), [1, 2])
            self.assertTrue(window.cards[2].is_required)
            self.assertFalse(window.cards[3].is_required)

            con.write_meta(root / con.META_NAME, {"__requirements__": [1, 2, 3]})
            window.reload_from_disk()

            self.assertEqual(list(window.get_effective_reqs()), [1, 2, 3])
            self.assertTrue(window.cards[3].is_required)
            window.close()

    def test_claude_theme_contract_markers_stay_stable(self):
        css = con.Theme.STYLESHEET
        self.assertEqual(con.Theme.PRIMARY, "#5C8DFF")
        self.assertEqual(con.Theme.BG_MAIN, "#1E2024")
        self.assertIn("QPlainTextEdit", css)
        self.assertIn("QTreeWidget", css)
        self.assertIn("QComboBox QAbstractItemView", css)
        self.assertIn(con.Theme.PRIMARY, css)
        self.assertIn("border-radius: 7px", con.MENU_STYLESHEET)
        self.assertIn("padding: 6px 24px 6px 10px", con.MENU_STYLESHEET)
        self.assertTrue(callable(con.configure_app_style))


if __name__ == "__main__":
    unittest.main()
