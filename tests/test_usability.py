import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import con


BOXES = ("LYGU3043781", "MSCU6639870", "TGHU1234566")


def write_photo(path: Path, color: str, size=(60, 80)) -> None:
    image = QImage(size[0], size[1], QImage.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path), "JPG")


def settle(app, seconds=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class UsabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(QThreadPool.globalInstance().waitForDone, 5000)
        config = patch.object(con, "CONFIG_PATH", Path(self.tmp.name) / "config.json")
        config.start()
        self.addCleanup(config.stop)
        self.root = Path(self.tmp.name) / "work"
        self.root.mkdir()
        for name in BOXES:
            (self.root / name).mkdir()
        con.write_meta(self.root / con.META_NAME, {"__requirements__": [1, 2, 3, 4, 5]})

    def window(self, size=(1366, 728)):
        window = con.ContainerApp()
        window.save_conf = lambda *_args, **_kwargs: True
        window.root = self.root
        window.refresh()
        window.resize(*size)
        window.show()
        settle(self.app)
        self.addCleanup(window.close)
        return window

    # --- task list -----------------------------------------------------
    def test_slot_strip_shows_done_missing_and_optional_slots(self):
        strip = con.SlotStrip({1: 2, 2: 0, 7: 1}, [1, 2, 6])
        self.addCleanup(strip.deleteLater)

        self.assertEqual(
            [strip.cell_state(idx) for idx in range(1, 9)],
            ["done", "missing", "optional", "optional", "optional", "missing", "done", "optional"],
        )
        self.assertEqual(strip.accessibleName(), "已有 1,7；缺 2,6")

    def test_task_rows_carry_a_slot_strip_when_counts_are_known(self):
        window = self.window()
        rows = [window.list.itemWidget(window.list.item(i)) for i in range(window.list.count())]
        rows = [row for row in rows if row is not None]

        self.assertTrue(rows)
        self.assertTrue(all(isinstance(row.slot_strip, con.SlotStrip) for row in rows))
        self.assertIsNone(con.TaskRowWidget({"name": "X", "pic_cnt": 0, "missing": []}).slot_strip)

    # --- detail page navigation -------------------------------------------
    def test_next_and_previous_box_follow_the_task_list_order(self):
        window = self.window()
        names = window._navigation_names()
        self.assertEqual(sorted(names), sorted(BOXES))

        window.open_box_name(names[0])
        detail = window._detail_page
        self.assertEqual(detail.lbl_position.text(), "1 / 3")
        self.assertFalse(detail.btn_prev_box.isEnabled())

        detail.btn_next_box.click()
        detail = window._detail_page
        self.assertEqual(detail.box_name, names[1])
        self.assertEqual(detail.lbl_position.text(), "2 / 3")
        self.assertEqual(window.list.currentItem().data(Qt.UserRole), names[1])

        QTest.keyClick(detail, Qt.Key_PageDown)
        detail = window._detail_page
        self.assertEqual(detail.box_name, names[2])
        self.assertFalse(detail.btn_next_box.isEnabled())

        toasts = []
        window.show_toast = lambda text, *_args, **_kwargs: toasts.append(text)
        window.open_adjacent_box(1)
        self.assertEqual(window._detail_page.box_name, names[2])
        self.assertEqual(toasts, ["已经是最后一个箱子"])

        window.open_adjacent_box(-1)
        self.assertEqual(window._detail_page.box_name, names[1])

    def test_switching_boxes_waits_while_photos_are_still_saving(self):
        window = self.window()
        names = window._navigation_names()
        window.open_box_name(names[0])
        detail = window._detail_page
        detail._image_import_queue.append((1, [("path", "x.jpg", "x.jpg")]))
        toasts = []
        window.show_toast = lambda text, *_args, **_kwargs: toasts.append(text)

        window.open_adjacent_box(1)

        self.assertIs(window._detail_page, detail)
        self.assertEqual(toasts, ["照片还在保存，保存完再切换箱子"])
        detail._image_import_queue.clear()

    # --- detail page layout ------------------------------------------------
    def test_all_eight_slots_fit_a_1366x768_screen_without_scrolling(self):
        window = self.window((1366, 728))
        window.open_box_name(BOXES[0])
        settle(self.app)
        detail = window._detail_page
        viewport = detail.scroll_area.viewport()

        self.assertFalse(detail.scroll_area.verticalScrollBar().isVisible())
        self.assertEqual(detail.cards[1].y(), detail.cards[4].y())
        self.assertGreater(detail.cards[5].y(), detail.cards[1].y())
        for card in detail.cards.values():
            self.assertLessEqual(card.geometry().bottom(), viewport.height())
            self.assertGreaterEqual(card.width(), 230)

    def test_small_window_scrolls_instead_of_squeezing_cards(self):
        window = self.window((900, 520))
        window.open_box_name(BOXES[0])
        settle(self.app)
        detail = window._detail_page

        self.assertGreaterEqual(detail.cards[1].height(), detail.min_card_h)
        self.assertGreater(detail.main_widget.height(), detail.scroll_area.viewport().height())

    def test_several_photos_in_a_slot_are_shown_side_by_side(self):
        box_dir = self.root / BOXES[0]
        two = [f"{BOXES[0]}-3.jpg", f"{BOXES[0]}-3_2.jpg"]
        three = [f"{BOXES[0]}-1.jpg", f"{BOXES[0]}-1_2.jpg", f"{BOXES[0]}-1_3.jpg"]
        for index, name in enumerate(two + three):
            write_photo(box_dir / name, f"#{index * 30 + 40:02x}5070")
        con.write_meta(self.root / con.META_NAME, {"__requirements__": [1, 2, 3], BOXES[0]: {"3": two, "1": three}})
        window = self.window((1366, 728))
        window.open_box_name(BOXES[0])
        settle(self.app)
        detail = window._detail_page

        self.assertEqual(detail.cards[3].photo_columns(), 2)
        self.assertEqual(detail.cards[3].lw.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        self.assertEqual(detail.cards[1].photo_columns(), 2)
        self.assertEqual(detail.cards[1].lw.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertIn("3 张", detail.cards[1].lbl_title.text())
        self.assertEqual(detail.cards[2].lbl_title.text(), "2 全开门1/2箱")

    # --- photo viewer -----------------------------------------------------
    def test_viewer_steps_through_the_box_and_zooms(self):
        paths = []
        for index, color in enumerate(("#aa0000", "#00aa00", "#0000aa")):
            path = Path(self.tmp.name) / f"p{index}.jpg"
            write_photo(path, color, (300, 400))
            paths.append(path)
        viewer = con.ImageViewer(str(paths[1]), photos=[(str(p), f"photo {i}") for i, p in enumerate(paths)], index=1)
        self.addCleanup(viewer.deleteLater)
        viewer.show()
        settle(self.app)

        self.assertIn("2 / 3", viewer.lbl_caption.fullText())
        QTest.keyClick(viewer, Qt.Key_Right)
        self.assertEqual(viewer.index, 2)
        self.assertFalse(viewer.btn_next.isEnabled())
        QTest.keyClick(viewer, Qt.Key_Right)
        self.assertEqual(viewer.index, 2)

        self.assertIsNone(viewer.zoom)
        viewer.set_zoom(3.0)
        settle(self.app, 0.05)
        self.assertEqual(viewer.lbl.pixmap().width(), 900)
        self.assertGreater(viewer.scroll.verticalScrollBar().maximum(), 0)
        viewer.set_zoom(None)
        self.assertIn("适应窗口", viewer.lbl_zoom.text())

    def test_preview_opens_on_the_clicked_photo_among_all_box_photos(self):
        box_dir = self.root / BOXES[0]
        names = {1: f"{BOXES[0]}-1.jpg", 4: f"{BOXES[0]}-4.jpg", 5: f"{BOXES[0]}-5.jpg"}
        for category, name in names.items():
            write_photo(box_dir / name, "#336699")
        con.write_meta(self.root / con.META_NAME, {BOXES[0]: {str(c): [n] for c, n in names.items()}})
        detail = con.BoxDetailWindow(self.root, BOXES[0])
        self.addCleanup(detail.prepare_for_disposal)
        opened = []

        class FakeViewer:
            def __init__(self, path, parent=None, photos=None, index=0):
                opened.append((photos, index))

            def exec(self):
                return 0

        lw = detail.cards[4].lw
        with patch.object(con, "ImageViewer", FakeViewer):
            detail.preview(lw.item(0), lw, 4)

        photos, index = opened[0]
        self.assertEqual([Path(path).name for path, _caption in photos], list(names.values()))
        self.assertEqual(index, 1)
        self.assertIn("4 半关门满箱", photos[1][1])


if __name__ == "__main__":
    unittest.main()
