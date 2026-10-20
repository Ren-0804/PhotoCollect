import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, Qt, QThreadPool, QUrl
from PySide6.QtGui import QColor, QDropEvent, QImage
from PySide6.QtWidgets import QApplication

import con
from photo_classifier import OTHER, PhotoPrediction
from storage_services import file_sha256


BOX = "LYGU3043781"


def predicted(category, confidence, runner_up=None):
    others = [c for c in (1, 2, 3, 4, 5, OTHER) if c != category]
    rest = (1.0 - confidence) / len(others)
    probabilities = {c: rest for c in others}
    if runner_up is not None:
        probabilities = {c: 0.0 for c in others}
        probabilities[runner_up] = 1.0 - confidence
    probabilities[category] = confidence
    return PhotoPrediction.from_probabilities(list(probabilities), list(probabilities.values()))


class FakeClassifier:
    """Answers by source file name; byte sources get `for_bytes`."""

    threshold = 0.9

    def __init__(self, by_name=None, for_bytes=None):
        self.by_name = dict(by_name or {})
        self.for_bytes = for_bytes
        self.samples = []

    def predict(self, sample):
        self.samples.append(sample)
        result = self.for_bytes if isinstance(sample, (bytes, bytearray)) else self.by_name[Path(sample).name]
        if isinstance(result, Exception):
            raise result
        return result


@contextmanager
def qt_temporary_directory():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            yield Path(tmp)
        finally:
            QThreadPool.globalInstance().waitForDone(5000)
            QApplication.processEvents()


def write_photo(folder: Path, name: str, color: str) -> Path:
    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(QColor(color))
    path = folder / name
    assert image.save(str(path), "JPG")
    return path


def file_drop(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    event = QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    return event, mime


class AutoSortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def open_detail(self, root, classifier):
        patcher = patch.object(con, "get_photo_classifier", return_value=classifier)
        patcher.start()
        self.addCleanup(patcher.stop)
        detail = con.BoxDetailWindow(root, BOX)
        self.addCleanup(detail.prepare_for_disposal)
        detail.maybe_auto_ocr = lambda *_args, **_kwargs: None
        self.notices = []
        detail._notify = lambda text, *_args: self.notices.append(text)
        return detail

    def stored(self, root):
        return {key: value for key, value in con.read_meta(root / con.META_NAME)[BOX].items() if key.isdigit() and value}

    def test_confident_photo_goes_to_the_predicted_slot(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "full.jpg", "#123456")
            detail = self.open_detail(root, FakeClassifier({"full.jpg": predicted(3, 0.97)}))

            detail.auto_sort_files([str(photo)])

            self.assertEqual(self.stored(root), {"3": [f"{BOX}-3.jpg"]})
            self.assertEqual(detail.meta["3"], [f"{BOX}-3.jpg"])
            self.assertEqual(file_sha256(root / BOX / f"{BOX}-3.jpg"), file_sha256(photo))
            self.assertIn("已自动放入 3 全开门满箱 1 张", self.notices[-1])

    def test_uncertain_photo_asks_and_uses_the_chosen_slot(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "half.jpg", "#224466")
            detail = self.open_detail(root, FakeClassifier({"half.jpg": predicted(2, 0.55, runner_up=3)}))
            asked = []
            detail._ask_auto_sort_category = lambda item: asked.append(item.prediction.candidates()) or 3

            detail.auto_sort_files([str(photo)])

            self.assertEqual(asked, [(2, 3, 1)])
            self.assertEqual(self.stored(root), {"3": [f"{BOX}-3.jpg"]})

    def test_skipping_an_uncertain_photo_saves_nothing(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "half.jpg", "#224466")
            detail = self.open_detail(root, FakeClassifier({"half.jpg": predicted(2, 0.55)}))
            detail._ask_auto_sort_category = lambda _item: None

            detail.auto_sort_files([str(photo)])

            self.assertEqual(list((root / BOX).glob("*.jpg")), [])
            self.assertEqual(detail.meta["2"], [])
            self.assertIn("跳过 1 张", self.notices[-1])

    def test_photo_of_slots_6_to_8_is_left_for_manual_sorting(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "beam.jpg", "#665544")
            detail = self.open_detail(root, FakeClassifier({"beam.jpg": predicted(OTHER, 0.96)}))
            detail._ask_auto_sort_category = lambda _item: self.fail("confident 6-8 photos are not asked")

            detail.auto_sort_files([str(photo)])

            self.assertEqual(list((root / BOX).glob("*.jpg")), [])
            self.assertIn("6-8 类", self.notices[-1])

    def test_occupied_slot_append_keeps_both_photos(self):
        with qt_temporary_directory() as root:
            first = write_photo(root, "first.jpg", "#101010")
            second = write_photo(root, "second.jpg", "#EEEEEE")
            detail = self.open_detail(root, FakeClassifier({"second.jpg": predicted(1, 0.99)}))
            detail.add_files(1, [str(first)])
            questions = []
            detail._ask_occupied_slot = lambda item, target, count: questions.append((item.label, target, count)) or "append"

            detail.auto_sort_files([str(second)])

            self.assertEqual(questions, [("second.jpg", 1, 1)])
            self.assertEqual(self.stored(root), {"1": [f"{BOX}-1.jpg", f"{BOX}-1_2.jpg"]})

    def test_occupied_slot_replace_moves_old_photo_to_trash_and_can_be_undone(self):
        with qt_temporary_directory() as root:
            old = write_photo(root, "old.jpg", "#101010")
            new = write_photo(root, "new.jpg", "#EEEEEE")
            detail = self.open_detail(root, FakeClassifier({"new.jpg": predicted(5, 0.99)}))
            detail.add_files(5, [str(old)])
            detail._ask_occupied_slot = lambda *_args: "replace"
            toasts = []
            detail._notify = lambda text, action="", callback=None: toasts.append((text, action, callback))

            detail.auto_sort_files([str(new)])

            names = self.stored(root)["5"]
            self.assertEqual(len(names), 1)
            self.assertEqual(file_sha256(root / BOX / names[0]), file_sha256(new))
            trash = con.TrashStore(root).list_entries()
            self.assertEqual([entry["category"] for entry in trash], ["5"])
            text, action, undo = toasts[-1]
            self.assertIn("旧照片已移到最近删除", text)
            self.assertEqual(action, "撤销替换")

            undo()

            names = self.stored(root)["5"]
            self.assertEqual([file_sha256(root / BOX / name) for name in names], [file_sha256(old)])
            self.assertEqual(len(con.TrashStore(root).list_entries()), 1)
            self.assertEqual(detail.meta["5"], names)

    def test_occupied_slot_skip_changes_nothing(self):
        with qt_temporary_directory() as root:
            old = write_photo(root, "old.jpg", "#101010")
            new = write_photo(root, "new.jpg", "#EEEEEE")
            detail = self.open_detail(root, FakeClassifier({"new.jpg": predicted(4, 0.99)}))
            detail.add_files(4, [str(old)])
            detail._ask_occupied_slot = lambda *_args: None

            detail.auto_sort_files([str(new)])

            self.assertEqual(self.stored(root), {"4": [f"{BOX}-4.jpg"]})
            self.assertEqual(file_sha256(root / BOX / f"{BOX}-4.jpg"), file_sha256(old))

    def test_two_photos_for_the_same_empty_slot_ask_about_the_second(self):
        with qt_temporary_directory() as root:
            a = write_photo(root, "a.jpg", "#101010")
            b = write_photo(root, "b.jpg", "#EEEEEE")
            detail = self.open_detail(root, FakeClassifier({"a.jpg": predicted(2, 0.95), "b.jpg": predicted(2, 0.97)}))
            questions = []
            detail._ask_occupied_slot = lambda item, target, count: questions.append((item.label, count)) or "append"

            detail.auto_sort_files([str(a), str(b)])

            self.assertEqual(questions, [("b.jpg", 1)])
            self.assertEqual(self.stored(root), {"2": [f"{BOX}-2.jpg", f"{BOX}-2_2.jpg"]})

    def test_photo_already_in_the_box_is_not_classified_again(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "empty.jpg", "#335577")
            classifier = FakeClassifier({"empty.jpg": predicted(3, 0.99)})
            detail = self.open_detail(root, classifier)
            detail.add_files(1, [str(photo)])

            detail.auto_sort_files([str(photo)])

            self.assertEqual(classifier.samples, [])
            self.assertEqual(self.stored(root), {"1": [f"{BOX}-1.jpg"]})
            self.assertIn("已在“1 全开门空箱”", self.notices[-1])

    def test_dragged_image_bytes_are_classified_and_saved_as_jpeg(self):
        with qt_temporary_directory() as root:
            payload = write_photo(root, "telegram.jpg", "#884422").read_bytes()
            classifier = FakeClassifier(for_bytes=predicted(4, 0.98))
            detail = self.open_detail(root, classifier)

            detail.auto_sort_virtual_files([("photo_2026.jpg", payload)])

            self.assertEqual(classifier.samples, [payload])
            self.assertEqual(self.stored(root), {"4": [f"{BOX}-4.jpg"]})

    def test_pasted_or_dragged_image_data_is_classified_as_the_jpeg_that_gets_saved(self):
        with qt_temporary_directory() as root:
            image = QImage(80, 60, QImage.Format_RGB32)
            image.fill(QColor("#4477AA"))
            classifier = FakeClassifier(for_bytes=predicted(2, 0.97))
            detail = self.open_detail(root, classifier)

            detail.auto_sort_image(image)

            self.assertEqual(len(classifier.samples), 1)
            self.assertEqual(classifier.samples[0], con.qimage_jpeg_bytes(image))
            self.assertEqual(self.stored(root), {"2": [f"{BOX}-2.jpg"]})

    def test_unreadable_photo_is_reported_and_the_rest_still_sorted(self):
        with qt_temporary_directory() as root:
            broken = root / "broken.jpg"
            broken.write_bytes(b"not a jpeg")
            good = write_photo(root, "good.jpg", "#AA5500")
            detail = self.open_detail(root, FakeClassifier({
                "broken.jpg": OSError("cannot identify image file"),
                "good.jpg": predicted(1, 0.99),
            }))

            detail.auto_sort_files([str(broken), str(good)])

            self.assertEqual(self.stored(root), {"1": [f"{BOX}-1.jpg"]})
            self.assertIn("1 张无法读取", self.notices[-1])

    def test_zone_accepts_image_drags_only_when_a_model_is_installed(self):
        with qt_temporary_directory() as root:
            detail = self.open_detail(root, FakeClassifier())
            self.assertTrue(detail.auto_sort_zone.acceptDrops())
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(root / "x.jpg"))])
            self.assertTrue(con.accepts_image_drag(mime))
            self.assertFalse(con.accepts_image_drag(QMimeData()))

    def test_drop_on_zone_sorts_and_drop_on_card_still_uses_that_card(self):
        with qt_temporary_directory() as root:
            auto = write_photo(root, "auto.jpg", "#AA0000")
            manual = write_photo(root, "manual.jpg", "#00AA00")
            detail = self.open_detail(root, FakeClassifier({"auto.jpg": predicted(1, 0.99)}))

            event, _mime = file_drop([auto])
            detail.auto_sort_zone.dropEvent(event)
            event, _mime = file_drop([manual])
            detail.cards[7].dropEvent(event)

            self.assertEqual(self.stored(root), {"1": [f"{BOX}-1.jpg"], "7": [f"{BOX}-7.jpg"]})

    def test_zone_is_disabled_without_a_model(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "any.jpg", "#123123")
            detail = self.open_detail(root, None)

            self.assertFalse(detail.auto_sort_zone.acceptDrops())
            self.assertFalse(detail.auto_sort_zone.btn_choose.isEnabled())
            detail.auto_sort_files([str(photo)])

            self.assertEqual(list((root / BOX).glob("*.jpg")), [])
            self.assertIn("自动分类不可用", self.notices[-1])

    def test_background_sort_classifies_off_the_ui_thread_then_imports(self):
        with qt_temporary_directory() as root:
            photo = write_photo(root, "closed.jpg", "#0000AA")
            detail = self.open_detail(root, FakeClassifier({"closed.jpg": predicted(5, 0.99)}))

            with patch.object(detail, "_use_background_image_import", return_value=True):
                detail.auto_sort_files([str(photo)])
                self.assertIsNotNone(detail._auto_sort_task)
                deadline = time.monotonic() + 10
                while (detail._auto_sort_task is not None or detail._image_import_task is not None) and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.01)
                QApplication.processEvents()

            self.assertIsNone(detail._auto_sort_task)
            self.assertEqual(self.stored(root), {"5": [f"{BOX}-5.jpg"]})
            self.assertEqual(detail.auto_sort_zone.lbl_hint.fullText(), con.AutoSortDropZone.IDLE_HINT)
            # The slot summary comes after the background save's own toasts.
            self.assertIn("后台已保存 1 张照片", self.notices)
            self.assertIn("已自动放入 5 全关门 1 张", self.notices[-1])

    def test_choice_dialog_offers_top_three_slots_and_any_slot(self):
        dialog = con.AutoSortChoiceDialog(QImage(), "x.jpg", predicted(OTHER, 0.5, runner_up=2))
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(list(dialog.candidate_buttons), [2, 1, 3])
        self.assertEqual(dialog.combo_other.count(), 8)
        dialog.candidate_buttons[2].click()
        self.assertEqual(dialog.chosen_category, 2)

    def test_data_health_warns_about_misplaced_photos_and_opens_their_box(self):
        with qt_temporary_directory() as root:
            box_dir = root / BOX
            box_dir.mkdir()
            names = {category: f"{BOX}-{category}.jpg" for category in (1, 3, 4, 7)}
            for category, name in names.items():
                write_photo(box_dir, name, "#335577")
            con.write_meta(root / con.META_NAME, {
                BOX: {str(category): [name] for category, name in names.items()},
            })
            classifier = FakeClassifier({
                names[1]: predicted(1, 0.99),
                names[3]: predicted(OTHER, 0.99),
                names[4]: predicted(4, 0.99),
                names[7]: predicted(4, 0.98),
            })
            threads = []
            original_predict = classifier.predict
            classifier.predict = lambda sample: threads.append(threading.get_ident()) or original_predict(sample)
            before = {path.name: path.stat().st_mtime_ns for path in box_dir.iterdir()}

            with patch.object(con, "get_photo_classifier", return_value=classifier):
                dialog = con.DataHealthDialog(root)
                self.addCleanup(dialog.deleteLater)
                dialog.show()
                deadline = time.monotonic() + 5
                while dialog.report is None and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.01)

            self.assertIsNotNone(dialog.report)
            self.assertEqual(dialog.report.error_count, 0)
            self.assertNotIn(threading.get_ident(), threads)
            rows = [dialog.results.topLevelItem(i) for i in range(dialog.results.topLevelItemCount())]
            flagged = [row for row in rows if row.text(1) == "疑似放错类别"]
            self.assertEqual(
                [row.text(2) for row in flagged],
                [
                    f"{BOX} · {names[3]} 放在 3 类（全开门满箱），看起来像 6-8 类（加固/底梁/铅封）（模型把握 99%）",
                    f"{BOX} · {names[7]} 放在 6-8 类（加固/底梁/铅封），看起来像 4 类（半关门满箱）（模型把握 98%）",
                ],
            )
            self.assertEqual({row.text(0) for row in flagged}, {"警告"})
            self.assertIn("模型已核对 4 张照片的类别", dialog.summary.text())
            self.assertEqual({path.name: path.stat().st_mtime_ns for path in box_dir.iterdir()}, before)

            dialog.results.itemDoubleClicked.emit(flagged[0], 2)

            self.assertEqual(dialog.selected_task_name, BOX)
            self.assertEqual(dialog.result(), con.QDialog.Accepted)


if __name__ == "__main__":
    unittest.main()
