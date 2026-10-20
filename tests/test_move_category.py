import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import con
from storage_services import file_sha256


BOX = "LYGU3043781"


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


def signature(path: Path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def trusted_ocr(path: Path) -> dict:
    return {
        "container_no": BOX,
        "verification_status": "match",
        "automatic": True,
        "recognized_at": "2026-10-20T10:00:00",
        "source_image": path.name,
        "source_images": [path.name],
        "source_sha256": {path.name: file_sha256(path)},
        "source_signatures": {path.name: signature(path)},
    }


class FakeAction:
    def __init__(self, text):
        self.text = text

    def setIcon(self, _icon):
        pass


def fake_menu_choosing(prefix, opened):
    """A QMenu stand-in whose exec() picks the first action, in any submenu, starting with `prefix`."""

    class FakeMenu:
        def __init__(self, *_args):
            self.title = ""
            self.actions = []
            self.submenus = []
            opened.append(self)

        def setStyleSheet(self, _style):
            pass

        def setIcon(self, _icon):
            pass

        def addAction(self, text):
            action = FakeAction(text)
            self.actions.append(action)
            return action

        def addMenu(self, title):
            menu = FakeMenu()
            menu.title = title
            self.submenus.append(menu)
            return menu

        def addSeparator(self):
            pass

        def exec(self, _position):
            for menu in (self, *self.submenus):
                for action in menu.actions:
                    if action.text.startswith(prefix):
                        return action
            return None

    return FakeMenu


class MoveCategoryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_box(self, root, slots, ocr=None):
        """Create the box folder with `slots` = {category: [(name, color)]} and matching meta."""
        box_dir = root / BOX
        box_dir.mkdir()
        paths = {}
        for category, photos in slots.items():
            for name, color in photos:
                paths[name] = write_photo(box_dir, name, color)

        def seed(all_data):
            box = {str(category): [name for name, _color in photos] for category, photos in slots.items()}
            if ocr is not None:
                box["__ocr__"] = ocr(paths)
            all_data[BOX] = box

        con.update_meta(root / con.META_NAME, seed)
        return paths

    def open_detail(self, root):
        detail = con.BoxDetailWindow(root, BOX)
        self.addCleanup(detail.close)
        self.addCleanup(detail.prepare_for_disposal)
        self.notices = []
        detail._notify = lambda text, action_text="", callback=None: self.notices.append(
            (text, action_text, callback)
        )
        self.ocr_calls = []
        detail.run_ocr_for_image = lambda name, automatic=False: self.ocr_calls.append((name, automatic))
        return detail

    def stored(self, root):
        return con.read_meta(root / con.META_NAME)[BOX]

    def slots(self, root):
        return {key: value for key, value in self.stored(root).items() if key.isdigit() and value}

    def disk_names(self, root):
        return sorted(path.name for path in (root / BOX).iterdir())

    def undo(self):
        text, action_text, callback = self.notices[-1]
        self.assertEqual(action_text, "撤销", text)
        callback()


class MoveToCategoryTests(MoveCategoryTestCase):
    def test_menu_lists_the_other_seven_categories_and_moves_the_photo(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]})
            detail = self.open_detail(root)
            card = detail.cards[4]
            point = card.lw.visualItemRect(card.lw.item(0)).center()
            opened = []

            with patch.object(con, "QMenu", fake_menu_choosing("3 ", opened)):
                detail.ctx(point, card.lw, 4)

            move_menu = next(menu for menu in opened if menu.title == "移到其他分类")
            expected = [label for idx, label in con.ALL_CATEGORIES if idx != 4]
            expected[expected.index(detail._category_label(5))] += "（已有 1 张）"
            self.assertEqual([action.text for action in move_menu.actions], expected)
            self.assertEqual(self.slots(root), {"3": [f"{BOX}-3.jpg"], "5": [f"{BOX}-5.jpg"]})
            self.assertEqual(detail.selected_category, 3)

    def test_move_renames_the_photo_and_updates_meta_and_hash_index(self):
        with qt_temporary_directory() as root:
            paths = self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            digest = file_sha256(paths[f"{BOX}-4.jpg"])
            detail = self.open_detail(root)
            self.assertEqual(detail._get_image_hash_index(), {digest: f"{BOX}-4.jpg"})

            self.assertTrue(detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3))

            self.assertEqual(self.disk_names(root), [f"{BOX}-3.jpg"])
            self.assertEqual(file_sha256(root / BOX / f"{BOX}-3.jpg"), digest)
            self.assertEqual(self.slots(root), {"3": [f"{BOX}-3.jpg"]})
            self.assertEqual(detail.meta["3"], [f"{BOX}-3.jpg"])
            self.assertEqual(detail.meta["4"], [])
            self.assertEqual(detail._get_image_hash_index(), {digest: f"{BOX}-3.jpg"})
            self.assertEqual(self.notices[-1][0], f"照片已移到“{detail._category_label(3)}”，改名为 {BOX}-3.jpg")

    def test_occupied_target_gets_the_next_free_name(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]})
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 5)

            self.assertEqual(self.slots(root), {"5": [f"{BOX}-5.jpg", f"{BOX}-5_2.jpg"]})
            self.assertEqual(self.disk_names(root), [f"{BOX}-5.jpg", f"{BOX}-5_2.jpg"])

    def test_undo_restores_the_original_name_and_slot(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]})
            detail = self.open_detail(root)
            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 5)

            self.undo()

            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg"], "5": [f"{BOX}-5.jpg"]})
            self.assertEqual(self.disk_names(root), [f"{BOX}-4.jpg", f"{BOX}-5.jpg"])
            self.assertEqual(self.notices[-1][:2], (f"已撤销，照片回到“{detail._category_label(4)}”", ""))

    def test_undo_uses_the_next_free_name_when_the_old_one_was_taken(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)
            undo = self.notices[-1][2]
            image = QImage(64, 48, QImage.Format_RGB32)
            image.fill(QColor("#aa2211"))
            detail.add_image_data(4, image)
            self.assertEqual(detail.meta["4"], [f"{BOX}-4.jpg"])

            undo()

            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg", f"{BOX}-4_2.jpg"]})

    def test_undo_after_the_photo_moved_again_changes_nothing(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)
            undo = self.notices[-1][2]
            detail._move_image_to_category(f"{BOX}-3.jpg", 3, 2)

            undo()

            self.assertEqual(self.slots(root), {"2": [f"{BOX}-2.jpg"]})
            self.assertEqual(self.notices[-1][0], "照片位置已经变化，没有撤销")

    def test_failed_meta_save_renames_the_photo_back(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            errors = []

            with patch.object(con, "update_meta", side_effect=OSError("read-only")), \
                    patch.object(con, "show_operation_error", lambda _parent, title, summary, _details="": errors.append((title, summary))):
                self.assertFalse(detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3))

            self.assertEqual(self.disk_names(root), [f"{BOX}-4.jpg"])
            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg"]})
            self.assertEqual(detail.meta["4"], [f"{BOX}-4.jpg"])
            self.assertEqual(detail.meta["3"], [])
            self.assertEqual(errors, [("照片移动失败", "分类记录没有保存，照片保持原名留在原分类。请检查工作目录是否可写。")])

    def test_failed_meta_save_and_failed_rename_back_reports_the_new_name(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            errors = []
            real_rename = os.rename
            renames = []

            def rename_once(src, dst):
                renames.append(Path(dst).name)
                if len(renames) > 1:
                    raise PermissionError(13, "in use")
                real_rename(src, dst)

            with patch.object(con, "update_meta", side_effect=OSError("read-only")), \
                    patch.object(con.os, "rename", rename_once), \
                    patch.object(con, "show_operation_error", lambda _parent, title, summary, _details="": errors.append(summary)):
                detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)

            self.assertEqual(renames, [f"{BOX}-3.jpg", f"{BOX}-4.jpg"])
            self.assertIn(f"现在叫 {BOX}-3.jpg", errors[0])
            # The page shows the file where its name puts it, as reopening would.
            self.assertEqual(detail.meta["3"], [f"{BOX}-3.jpg"])
            self.assertEqual(detail.meta["4"], [])

    def test_locked_photo_is_left_untouched(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            errors = []

            with patch.object(con.os, "rename", side_effect=PermissionError(13, "in use")), \
                    patch.object(con, "show_operation_error", lambda _parent, title, *_rest: errors.append(title)):
                self.assertFalse(detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3))

            self.assertEqual(errors, ["照片移动失败"])
            self.assertEqual(self.disk_names(root), [f"{BOX}-4.jpg"])
            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg"]})

    def test_photo_with_its_own_name_moves_without_renaming(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {2: [("IMG_0001.jpg", "#335577")]})
            detail = self.open_detail(root)

            detail._move_image_to_category("IMG_0001.jpg", 2, 3)

            self.assertEqual(self.slots(root), {"3": ["IMG_0001.jpg"]})
            self.assertEqual(self.disk_names(root), ["IMG_0001.jpg"])
            self.assertEqual(self.notices[-1][0], f"照片已移到“{detail._category_label(3)}”")

    def test_photo_already_named_for_the_target_keeps_its_name(self):
        with qt_temporary_directory() as root:
            # Left behind by older versions, which moved photos without renaming.
            self.make_box(root, {4: [(f"{BOX}-5.jpg", "#335577")]})
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-5.jpg", 4, 5)

            self.assertEqual(self.slots(root), {"5": [f"{BOX}-5.jpg"]})

    def test_move_waits_while_the_photo_is_being_recognized(self):
        with qt_temporary_directory() as root:
            paths = self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            detail._ocr_task = SimpleNamespace(image_path=str(paths[f"{BOX}-4.jpg"]))

            self.assertFalse(detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3))

            self.assertEqual(self.notices[-1][0], "这张照片正在识别，请稍后再移动")
            self.assertEqual(self.disk_names(root), [f"{BOX}-4.jpg"])
            detail._ocr_task = None

    def test_undo_waits_for_a_running_import_and_offers_undo_again(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]})
            detail = self.open_detail(root)
            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)
            undo = self.notices[-1][2]
            detail._image_import_queue.append((1, [("path", "x.jpg", "x.jpg")]))

            undo()

            self.assertEqual(self.notices[-1][:2], ("照片还在保存，请稍后再撤销", "撤销"))
            self.assertEqual(self.slots(root), {"3": [f"{BOX}-3.jpg"]})
            detail._image_import_queue.clear()
            self.undo()
            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg"]})


class MoveToCategoryOcrTests(MoveCategoryTestCase):
    """OCR evidence follows the photo between 4 and 5 and is retired when it leaves them."""

    def test_moving_the_ocr_photo_between_door_slots_keeps_the_trusted_result(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]},
                          ocr=lambda paths: trusted_ocr(paths[f"{BOX}-4.jpg"]))
            detail = self.open_detail(root)
            self.assertTrue(detail.has_trusted_ocr_result())

            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 5)

            self.assertEqual(self.ocr_calls, [])
            self.assertTrue(detail.has_trusted_ocr_result())
            self.assertEqual(detail.ocr_result["source_images"], [f"{BOX}-5.jpg"])
            stored = self.stored(root)["__ocr__"]
            self.assertEqual(stored["source_image"], f"{BOX}-5.jpg")
            self.assertEqual(list(stored["source_signatures"]), [f"{BOX}-5.jpg"])
            self.assertEqual(list(stored["source_sha256"]), [f"{BOX}-5.jpg"])
            reopened = con.BoxDetailWindow(root, BOX)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.ocr_result["verification_status"], "match")

    def test_moving_the_ocr_photo_out_rechecks_the_remaining_door_photo(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]},
                          ocr=lambda paths: trusted_ocr(paths[f"{BOX}-4.jpg"]))
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)

            self.assertEqual(self.ocr_calls, [(f"{BOX}-5.jpg", True)])
            self.assertEqual(detail.ocr_result["pending_source"], f"{BOX}-5.jpg")
            self.assertEqual(detail.ocr_result["verification_status"], "unreadable")
            self.assertEqual(self.stored(root)["__ocr__"]["pending_source"], f"{BOX}-5.jpg")

    def test_moving_the_only_door_photo_out_clears_ocr_and_undo_restores_it(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")]},
                          ocr=lambda paths: trusted_ocr(paths[f"{BOX}-4.jpg"]))
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)

            self.assertEqual(detail.ocr_result, {})
            self.assertNotIn("__ocr__", self.stored(root))
            self.assertEqual(detail.lbl_ocr_status.text(), "OCR 待识别")

            self.undo()

            self.assertEqual(self.ocr_calls, [])
            self.assertEqual(self.slots(root), {"4": [f"{BOX}-4.jpg"]})
            self.assertTrue(detail.has_trusted_ocr_result())
            self.assertEqual(self.stored(root)["__ocr__"]["source_images"], [f"{BOX}-4.jpg"])

    def test_undo_after_a_new_recognition_rechecks_the_returning_photo(self):
        with qt_temporary_directory() as root:
            paths = self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]},
                                  ocr=lambda paths: trusted_ocr(paths[f"{BOX}-4.jpg"]))
            detail = self.open_detail(root)
            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)
            detail.ocr_result = trusted_ocr(paths[f"{BOX}-5.jpg"])

            self.undo()

            self.assertEqual(self.ocr_calls, [(f"{BOX}-5.jpg", True), (f"{BOX}-4.jpg", True)])
            self.assertEqual(detail.ocr_result["pending_source"], f"{BOX}-4.jpg")

    def test_moving_a_photo_into_a_door_slot_recognizes_it(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {3: [(f"{BOX}-3.jpg", "#335577")]})
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-3.jpg", 3, 5)

            self.assertEqual(self.ocr_calls, [(f"{BOX}-5.jpg", True)])
            self.assertEqual(detail.ocr_result["pending_source"], f"{BOX}-5.jpg")

    def test_moving_another_photo_out_of_a_door_slot_keeps_the_result(self):
        with qt_temporary_directory() as root:
            self.make_box(root, {4: [(f"{BOX}-4.jpg", "#335577")], 5: [(f"{BOX}-5.jpg", "#557733")]},
                          ocr=lambda paths: trusted_ocr(paths[f"{BOX}-5.jpg"]))
            detail = self.open_detail(root)

            detail._move_image_to_category(f"{BOX}-4.jpg", 4, 3)

            self.assertEqual(self.ocr_calls, [])
            self.assertTrue(detail.has_trusted_ocr_result())
            self.assertEqual(detail.ocr_result["source_images"], [f"{BOX}-5.jpg"])


if __name__ == "__main__":
    unittest.main()
