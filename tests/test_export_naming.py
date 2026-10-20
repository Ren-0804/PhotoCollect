import unittest
from datetime import datetime

from export_naming import (
    DEFAULT_EXPORT_TEMPLATES,
    render_export_filename,
)


class ExportNamingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 31, 9, 8)

    def test_custom_zip_template_renders_business_placeholders(self):
        result = render_export_filename(
            "zip",
            "【交接包】*{YYYYMMDD}*{箱数}箱",
            task_count=12,
            photo_count=86,
            scope="所选任务",
            now=self.now,
        )

        self.assertEqual(result.filename, "【交接包】2026073112箱.zip")
        self.assertFalse(result.used_fallback)

    def test_excel_template_supports_photo_count_scope_and_timestamp(self):
        result = render_export_filename(
            "excel",
            "{范围}_{YYYYMMDD_HHMM}_{照片数}张.xlsx",
            task_count=3,
            photo_count=19,
            scope="仅已完成任务",
            now=self.now,
        )

        self.assertEqual(result.filename, "仅已完成任务_20260731_0908_19张.xlsx")
        self.assertFalse(result.used_fallback)

    def test_unknown_placeholder_uses_the_legacy_default(self):
        result = render_export_filename(
            "zip",
            "{客户名}_{YYYYMMDD}.zip",
            task_count=1,
            photo_count=2,
            scope="全部任务",
            now=self.now,
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.filename, "PhotoCollect_20260731_0908.zip")
        self.assertIn("客户名", result.error)

    def test_path_separator_or_reserved_windows_name_falls_back(self):
        traversal = render_export_filename(
            "zip",
            "..\\outside\\{YYYYMMDD}.zip",
            task_count=1,
            photo_count=1,
            scope="全部任务",
            now=self.now,
        )
        reserved = render_export_filename(
            "excel",
            "CON.xlsx",
            task_count=1,
            photo_count=1,
            scope="全部任务",
            now=self.now,
        )

        self.assertTrue(traversal.used_fallback)
        self.assertTrue(reserved.used_fallback)
        self.assertEqual(
            reserved.filename,
            "PhotoCollect_采集看板_20260731_0908.xlsx",
        )

    def test_wrong_or_missing_extension_is_normalized_for_the_kind(self):
        missing = render_export_filename(
            "zip",
            "交接包_{箱数}箱",
            task_count=2,
            photo_count=8,
            scope="全部任务",
            now=self.now,
        )
        wrong = render_export_filename(
            "excel",
            "看板_{YYYYMMDD}.zip",
            task_count=2,
            photo_count=8,
            scope="全部任务",
            now=self.now,
        )

        self.assertEqual(missing.filename, "交接包_2箱.zip")
        self.assertEqual(wrong.filename, "看板_20260731.xlsx")
        self.assertFalse(missing.used_fallback)
        self.assertFalse(wrong.used_fallback)

    def test_defaults_remain_the_existing_public_filename_contract(self):
        zip_name = render_export_filename(
            "zip",
            DEFAULT_EXPORT_TEMPLATES["zip"],
            task_count=0,
            photo_count=0,
            scope="全部任务",
            now=self.now,
        )
        excel_name = render_export_filename(
            "excel",
            DEFAULT_EXPORT_TEMPLATES["excel"],
            task_count=0,
            photo_count=0,
            scope="全部任务",
            now=self.now,
        )

        self.assertEqual(zip_name.filename, "PhotoCollect_20260731_0908.zip")
        self.assertEqual(
            excel_name.filename,
            "PhotoCollect_采集看板_20260731_0908.xlsx",
        )


if __name__ == "__main__":
    unittest.main()
