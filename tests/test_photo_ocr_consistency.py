import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
import con


class PhotoOcrConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.name = 'LYGU3043781'
        self.window = con.BoxDetailWindow(self.root, self.name)

    def tearDown(self):
        self.window.prepare_for_disposal()
        self.window.close()
        con.QThreadPool.globalInstance().waitForDone(5000)
        QApplication.processEvents()
        self.tmp.cleanup()

    def test_ocr_save_does_not_erase_concurrent_photo_import(self):
        photo = 'camera-original.jpg'
        (self.window.box_dir / photo).write_bytes(b'photo')
        con.update_meta(self.window.meta_path, lambda data: data.setdefault(self.name, {}).update({'5': [photo]}))
        self.window.ocr_result = {'container_no': self.name, 'size_type': '22G1'}
        self.assertTrue(self.window.save_meta())
        self.window.reload_from_disk()
        self.assertIn(photo, self.window.meta['5'])
        self.assertEqual(self.window.cards[5].lw.count(), 1)

    def test_partial_refresh_includes_other_changed_categories(self):
        photo = f'{self.name}-5.jpg'
        (self.window.box_dir / photo).write_bytes(b'photo')
        self.window.reload_from_disk(categories={1})
        self.assertIn(photo, self.window.meta['5'])
        self.assertEqual(self.window.cards[5].lw.count(), 1)

    def test_transient_stat_failure_never_deletes_registered_photo(self):
        photo = 'camera-original.jpg'
        path = self.window.box_dir / photo
        path.write_bytes(b'photo')
        con.update_meta(self.window.meta_path, lambda data: data.setdefault(self.name, {}).update({'5': [photo]}))
        original = Path.stat
        def fail_photo(p, *args, **kwargs):
            if p.name == photo:
                raise PermissionError('temporary share failure')
            return original(p, *args, **kwargs)
        with patch.object(Path, 'stat', fail_photo):
            self.window.reload_from_disk()
        self.assertIn(photo, con.read_meta(self.window.meta_path)[self.name]['5'])
        self.window.reload_from_disk()
        self.assertEqual(self.window.cards[5].lw.count(), 1)

    def test_cached_status_is_recomputed_in_both_directions(self):
        other = 'MSCU6639870'
        for recognized, saved, expected in [(other, 'match', 'mismatch'), (self.name, 'mismatch', 'match')]:
            with self.subTest(recognized=recognized):
                summary = con.build_task_summary(self.window.box_dir, {'__ocr__': {
                    'container_no': recognized, 'verification_status': saved,
                }}, [])
                self.assertEqual(summary['ocr_status'], expected)

    def test_new_door_photo_is_not_skipped_after_old_match(self):
        self.window.ocr_result = {'container_no': self.name, 'verification_status': 'match', 'source_image': 'old.jpg'}
        with patch.object(self.window, 'run_ocr_for_image') as run:
            self.window.maybe_auto_ocr(5, ['new.jpg'])
        run.assert_called_once_with('new.jpg', automatic=True)

    def test_deleted_photo_ocr_callback_cannot_mark_task_matched(self):
        self.window.on_ocr_finished({'container_no': self.name, 'size_type': '22G1'}, 'deleted.jpg', True)
        self.assertNotEqual(self.window.ocr_result.get('verification_status'), 'match')

    def test_same_filename_replaced_after_recognition_is_not_trusted(self):
        path = self.window.box_dir / 'door.jpg'
        path.write_bytes(b'old photo')
        with patch.object(con, 'run_ocr_image', return_value={'container_no': self.name, 'size_type': '22G1'}):
            result = con.run_bound_ocr(path)
        path.write_bytes(b'new different photo')
        self.window.ocr_result = result
        self.assertFalse(self.window.has_trusted_ocr_result())
        self.assertEqual(self.window.ocr_result['size_type'], '')

    def test_current_photo_callback_saves_correct_match_and_mismatch(self):
        path = self.window.box_dir / f'{self.name}-5.jpg'
        path.write_bytes(b'photo')
        self.window.reload_from_disk()
        for number, expected in [(self.name, 'match'), ('MSCU6639870', 'mismatch')]:
            with patch.object(con, 'run_ocr_image', return_value={'container_no': number, 'size_type': '22G1'}):
                result = con.run_bound_ocr(path)
            self.window.on_ocr_finished(result, path.name, True)
            self.assertEqual(self.window.ocr_result['verification_status'], expected)
            self.assertEqual(con.build_task_summary(self.window.box_dir, con.read_meta(self.window.meta_path)[self.name], [])['ocr_status'], expected)

    def test_concurrent_import_into_same_category_is_preserved(self):
        self.window.meta['5'] = ['local.jpg']
        con.update_meta(self.window.meta_path, lambda data: data.setdefault(self.name, {}).update({'5': ['background.jpg']}))
        self.window.save_meta()
        self.assertEqual(set(con.read_meta(self.window.meta_path)[self.name]['5']), {'local.jpg', 'background.jpg'})

    def test_conflicting_door_types_are_not_silently_confirmed(self):
        merged = con.merge_important_ocr_results([
            {'container_no': self.name, 'size_type': '22G1'},
            {'container_no': self.name, 'size_type': '25G1'},
        ])
        checked = con.current_ocr_result(merged, self.name)
        self.assertEqual(checked['verification_status'], 'unreadable')
        self.assertIn('冲突', checked['verification_reason'])

    def test_real_background_import_displays_pixels_in_fifth_and_eighth_cards(self):
        self.window.resize(1024, 700)
        self.window.show()
        QApplication.processEvents()
        with patch.object(self.window, '_use_background_image_import', return_value=True), \
                patch.object(self.window, 'maybe_auto_ocr'):
            for category, color in [(5, 'red'), (8, 'green')]:
                image = QImage(160, 120, QImage.Format_RGB32)
                image.fill(QColor(color))
                self.window.add_image_data(category, image)
                widget = None
                for _ in range(300):
                    QApplication.processEvents()
                    card = self.window.cards[category]
                    if card.lw.count():
                        widget = card.lw.itemWidget(card.lw.item(0))
                        if widget.pm is not None and not widget.pm.isNull():
                            break
                    QTest.qWait(10)
                self.assertIsNotNone(widget)
                self.assertIsNotNone(widget.pm)
                self.assertFalse(widget.lbl.pixmap().isNull())
                self.assertGreater(widget.lbl.width(), 10)
                self.assertGreater(widget.lbl.height(), 10)

    def test_replaced_photo_late_callback_is_discarded(self):
        path = self.window.box_dir / f'{self.name}-5.jpg'
        path.write_bytes(b'old')
        self.window.reload_from_disk()
        with patch.object(con, 'run_ocr_image', return_value={'container_no': self.name, 'size_type': '22G1'}):
            result = con.run_bound_ocr(path)
        path.write_bytes(b'replacement')
        self.window.on_ocr_finished(result, path.name, True)
        self.assertNotEqual(self.window.ocr_result.get('verification_status'), 'match')

    def test_background_save_after_return_still_updates_home_task(self):
        from types import SimpleNamespace
        with patch.object(con, 'CONFIG_PATH', self.root / 'config.json'):
            host = con.ContainerApp()
        host.root = self.root
        host.refresh()
        host._detail_page = None
        path = self.window.box_dir / f'{self.name}-5.jpg'
        path.write_bytes(b'photo')
        host._on_background_import_committed(self.root, self.name, SimpleNamespace(added_names=(path.name,)))
        QApplication.processEvents()
        summary = next(item for item in host.task_data if item['name'] == self.name)
        self.assertEqual(summary['pic_cnt'], 1)
        host.close()


if __name__ == '__main__':
    unittest.main()
