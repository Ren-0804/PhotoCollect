import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

import con
from updater_services import UpdateCheckResult, parse_update_manifest


def available_update():
    digest = "a" * 64
    manifest = parse_update_manifest(
        {
            "schema_version": 1,
            "app": "PhotoCollect",
            "channel": "stable",
            "version": "5.16.3",
            "release_id": f"PhotoCollect:5.16.3:{digest}",
            "published_at": "2026-07-31T08:20:13+00:00",
            "notes": "测试更新说明",
            "file": {
                "name": "PhotoCollect-v5.16.3.exe",
                "url": "http://192.168.110.79:18080/PhotoCollect-v5.16.3.exe",
                "size_bytes": 1024,
                "sha256": digest,
            },
        },
        manifest_url="http://192.168.110.79:18080/latest.json",
    )
    return UpdateCheckResult(
        current_version=con.APP_VERSION,
        manifest=manifest,
        available=True,
    )


class UpdaterUiTests(unittest.TestCase):
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

    def make_window(self):
        window = con.ContainerApp()
        self.addCleanup(window.close)
        return window

    def test_sidebar_exposes_check_update_before_about(self):
        window = self.make_window()
        layout = window.sidebar.layout()

        self.assertLess(
            layout.indexOf(window.btn_check_update),
            layout.indexOf(window.btn_about),
        )
        self.assertIn(window.btn_check_update, window._sidebar_buttons)

    def test_main_window_uses_requested_product_name(self):
        window = self.make_window()

        self.assertEqual(con.APP_DISPLAY_NAME, "集装箱照片收集工具")
        self.assertEqual(window.app_title.text(), con.APP_DISPLAY_NAME)
        self.assertEqual(
            window.windowTitle(),
            f"{con.APP_DISPLAY_NAME} v{con.APP_VERSION}",
        )

    def test_about_dialog_uses_requested_product_name(self):
        window = self.make_window()

        with patch.object(QDialog, "exec", return_value=0):
            window.show_about()

        dialog = window.findChildren(QDialog)[-1]
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertEqual(dialog.windowTitle(), f"关于 {con.APP_DISPLAY_NAME}")
        self.assertIn(con.APP_DISPLAY_NAME, labels)

    def test_update_notes_use_requested_product_name(self):
        window = self.make_window()
        release = available_update().manifest

        with patch.object(QMessageBox, "information") as information:
            window.show_update_notes(release)

        self.assertEqual(
            information.call_args.args[1],
            f"{con.APP_DISPLAY_NAME} v{release.version}",
        )

    def test_manual_check_is_dispatched_to_the_background_pool(self):
        window = self.make_window()
        pool = Mock()
        window._update_thread_pool = pool

        window.check_for_updates(manual=True)

        pool.start.assert_called_once_with(window._update_check_task)
        self.assertFalse(window.btn_check_update.isEnabled())
        self.assertEqual(window.btn_check_update.text(), "检查中...")

    def test_close_cancels_and_joins_the_update_worker_pool(self):
        window = self.make_window()
        download = Mock()
        pool = Mock()
        pool.waitForDone.return_value = True
        window._update_download_task = download
        window._update_thread_pool = pool

        window.closeEvent(QCloseEvent())

        download.cancel.assert_called_once_with()
        pool.clear.assert_called_once_with()
        pool.waitForDone.assert_called_once()
        self.assertTrue(window._closing)

    def test_new_release_uses_existing_toast_with_install_and_notes_actions(self):
        window = self.make_window()
        task = object()
        window._update_check_task = task

        with patch.object(window, "show_toast") as show_toast:
            window._on_update_check_finished(
                task,
                available_update(),
                manual=False,
            )

        self.assertIsNone(window._update_check_task)
        self.assertEqual(window._available_update.version, "5.16.3")
        args = show_toast.call_args.args
        self.assertIn("发现新版本 v5.16.3", args[0])
        self.assertEqual(args[1], "立即更新")
        self.assertTrue(callable(args[2]))
        self.assertEqual(show_toast.call_args.kwargs["secondary_text"], "更新说明")
        self.assertTrue(
            callable(show_toast.call_args.kwargs["secondary_callback"])
        )


if __name__ == "__main__":
    unittest.main()
