import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

import con


def task_summary(name: str, *, ocr_status: str = "match") -> dict:
    return {
        "name": name,
        "pic_cnt": 6,
        "missing": [],
        "ok": True,
        "task_state": "complete",
        "ocr_container": name,
        "ocr_size_type": "25G1",
        "ocr_status": ocr_status,
        "box_number_valid": True,
        "has_auto_ocr_photo": True,
        "ocr_enabled": True,
        "updated_ts": 0.0,
        "last_updated": "",
        "filter": f"{name.lower()} 25g1",
    }


class DashboardResponsivenessTests(unittest.TestCase):
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

    def make_window(self) -> con.ContainerApp:
        window = con.ContainerApp()
        self.addCleanup(window.close)
        return window

    def test_task_row_badges_share_one_vertical_center(self):
        row = con.TaskRowWidget(
            task_summary("CICU7344386", ocr_status="inferred_match")
        )
        self.addCleanup(row.close)
        row.resize(974, 64)
        row.show()
        self.app.processEvents()

        labels = {
            label.text(): label
            for label in row.findChildren(QLabel)
            if label.text() in {"推算吻合", "完成"}
        }
        self.assertEqual(set(labels), {"推算吻合", "完成"})
        self.assertEqual(
            labels["推算吻合"].geometry().center().y(),
            labels["完成"].geometry().center().y(),
        )

    def test_task_row_keeps_box_number_and_size_type_in_stable_columns(self):
        row = con.TaskRowWidget(task_summary("CICU7344386"))
        self.addCleanup(row.close)
        row.resize(974, 64)
        row.show()
        self.app.processEvents()

        self.assertEqual(row.container_label.text(), "CICU7344386")
        self.assertEqual(row.size_type_label.text(), "25G1")
        self.assertEqual(row.size_type_label.width(), 64)
        self.assertGreaterEqual(row.container_label.minimumWidth(), 145)
        self.assertLessEqual(row.container_label.maximumWidth(), 175)
        self.assertIn("CICU7344386", row.accessible_title_label.text())
        self.assertIn("25G1", row.accessible_title_label.text())

    def test_size_type_badges_use_quiet_distinct_states(self):
        rows = [
            con.TaskRowWidget({**task_summary("CICU7344386"), "ocr_size_type": "22G1"}),
            con.TaskRowWidget({**task_summary("CICU7344405"), "ocr_size_type": "25G1"}),
            con.TaskRowWidget({**task_summary("CICU7344426"), "ocr_size_type": ""}),
        ]
        for row in rows:
            self.addCleanup(row.close)

        self.assertEqual(rows[0].size_type_label.property("badge_state"), "info")
        self.assertEqual(rows[1].size_type_label.property("badge_state"), "ok")
        self.assertEqual(rows[2].size_type_label.property("badge_state"), "warn")
        self.assertEqual(
            len({row.size_type_label.styleSheet() for row in rows}),
            3,
        )

    def test_render_list_defers_offscreen_row_widget_construction(self):
        window = self.make_window()
        window.task_data = [
            task_summary(f"CICU{index:07d}")
            for index in range(500)
        ]
        original_row_widget = con.TaskRowWidget

        with patch.object(
            con,
            "TaskRowWidget",
            side_effect=lambda data, parent=None: original_row_widget(data, parent),
        ) as row_factory:
            # render_list runs entirely on the UI thread, so measure that
            # thread's CPU time. Wall time breaks when Windows deschedules the
            # test process; process_time() also counts other threads, such as
            # the ONNX Runtime workers that keep spinning for about a second
            # after the OCR acceptance test that runs just before this one.
            started = time.thread_time()
            window.render_list()
            elapsed_ms = (time.thread_time() - started) * 1000

            self.assertEqual(window.list.count(), 500)
            self.assertLess(row_factory.call_count, 100)
            self.assertLess(elapsed_ms, 250)

    def test_verified_remote_refresh_scans_outside_the_ui_thread(self):
        window = self.make_window()
        root = Path(self.config_dir.name) / "remote-work"
        root.mkdir()
        names = [f"CICU{index:07d}" for index in range(20)]
        for name in names:
            (root / name).mkdir()
        con.write_meta(root / con.META_NAME, {"__requirements__": [1, 4, 5]})

        window.root = root
        window._verified_remote_root = str(root)
        scan_started = threading.Event()
        release_scan = threading.Event()
        original_scan = getattr(con, "scan_task_summaries", None)

        def delayed_scan(root_path, require_ocr=True):
            scan_started.set()
            release_scan.wait(2)
            if original_scan is None:
                return {}
            return original_scan(root_path, require_ocr=require_ocr)

        try:
            with (
                patch.object(con, "is_remote_work_root", return_value=True),
                patch.object(
                    con,
                    "scan_task_summaries",
                    side_effect=delayed_scan,
                    create=True,
                ),
            ):
                started = time.perf_counter()
                window.refresh()
                elapsed_ms = (time.perf_counter() - started) * 1000

                self.assertTrue(scan_started.wait(0.5))
                self.assertLess(elapsed_ms, 250)
                release_scan.set()
                deadline = time.monotonic() + 5
                while (
                    getattr(window, "_task_scan_task", None) is not None
                    and time.monotonic() < deadline
                ):
                    self.app.processEvents()
                    time.sleep(0.01)
                self.app.processEvents()
        finally:
            release_scan.set()
            con.QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()

        self.assertEqual(
            {data["name"] for data in window.task_data},
            set(names),
        )

    def test_stale_remote_scan_result_cannot_replace_new_root(self):
        window = self.make_window()
        old_root = Path(self.config_dir.name) / "old-root"
        new_root = Path(self.config_dir.name) / "new-root"
        window.root = new_root
        window._task_scan_generation = 2
        expected = task_summary("CICU7344386")
        window.task_data = [expected]
        stale = {
            "task_data": [task_summary("MSCU6639870")],
            "ok_count": 1,
            "state_counts": {
                "all": 1,
                "pending": 0,
                "anomaly": 0,
                "complete": 1,
            },
        }

        window._on_task_scan_finished(
            1,
            str(old_root),
            stale,
            "",
        )

        self.assertEqual(window.root, new_root)
        self.assertEqual(window.task_data, [expected])

    def test_window_keeps_the_config_target_captured_at_creation(self):
        original_target = Path(self.config_dir.name) / "config.json"
        alternate_target = Path(self.config_dir.name) / "alternate.json"
        window = con.ContainerApp()
        with patch.object(con, "CONFIG_PATH", alternate_target):
            window.save_conf()
            window.close()

        self.assertTrue(original_target.is_file())
        self.assertFalse(alternate_target.exists())


if __name__ == "__main__":
    unittest.main()
