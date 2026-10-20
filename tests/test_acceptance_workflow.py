import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import con
from acceptance_workflow import run_acceptance


class AcceptanceWorkflowTests(unittest.TestCase):
    fixture = Path(__file__).resolve().parent / "fixtures" / "container_sample.jpg"

    def test_isolated_source_workflow_writes_success_report_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            report_path = base / "acceptance.json"
            workspace = base / "workspace"

            exit_code = run_acceptance(
                con,
                report_path,
                workspace=workspace,
                ocr_fixture=self.fixture,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertTrue(report["ok"])
            self.assertEqual(report["version"], con.APP_VERSION)
            self.assertGreaterEqual(len(report["steps"]), 10)
            self.assertTrue(all(step["ok"] for step in report["steps"]))
            self.assertTrue((workspace / "artifacts" / "handoff.zip").is_file())
            self.assertTrue((workspace / "artifacts" / "collection.xlsx").is_file())
            self.assertTrue(report["zip_validation"]["flat_container_layout"])
            self.assertTrue(report["zip_validation"]["manifest_matches_files"])
            self.assertTrue(report["runtime"]["executable"])
            self.assertGreater(report["runtime"]["size_bytes"], 0)
            self.assertEqual(len(report["runtime"]["sha256"]), 64)
            ocr_step = next(step for step in report["steps"] if step["name"] == "ocr_fixture")
            self.assertEqual(ocr_step["detail"]["container_no"], con.OCR_REGRESSION_CONTAINER_NO)
            self.assertEqual(ocr_step["detail"]["size_type"], con.OCR_REGRESSION_SIZE_TYPE)
            self.assertTrue(ocr_step["detail"]["check_digit_valid"])

    def test_acceptance_without_ocr_fixture_fails_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            report_path = base / "acceptance.json"

            exit_code = run_acceptance(
                con,
                report_path,
                workspace=base / "workspace",
                ocr_fixture=None,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(report["ok"])
            ocr_step = next(step for step in report["steps"] if step["name"] == "ocr_fixture")
            self.assertFalse(ocr_step["ok"])
            self.assertIn("OCR固定样图未提供", ocr_step["error"])

    def test_default_acceptance_workspace_is_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "acceptance.json"

            exit_code = run_acceptance(
                con,
                report_path,
                ocr_fixture=None,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertFalse(Path(report["workspace"]).exists())

    def test_release_pipeline_requires_real_ocr_and_cleans_acceptance_workspaces(self):
        script = (
            Path(__file__).resolve().parent.parent / "一键构建_PhotoCollect.bat"
        ).read_text(encoding="utf-8")

        ocr_check = '%PY% -c "import rapidocr_onnxruntime"'
        full_suite = "%PY% -m unittest discover -s tests -v"
        self.assertIn(ocr_check, script)
        self.assertLess(script.index(ocr_check), script.index(full_suite))
        self.assertGreaterEqual(
            script.count('if exist "%SOURCE_WORK%" rd /s /q "%SOURCE_WORK%"'),
            2,
        )
        self.assertGreaterEqual(
            script.count('if exist "%EXE_WORK%" rd /s /q "%EXE_WORK%"'),
            2,
        )
        self.assertIn("APP_VERSION", script)
        self.assertIn("acceptance-v%VERSION%-package-", script)
        self.assertIn("Security.Cryptography.SHA256", script)
        self.assertNotIn("Get-FileHash", script)
        process_check = 'tasklist /FI "IMAGENAME eq PhotoCollect.exe"'
        self.assertIn(process_check, script)
        self.assertIn(":fail_desktop_running", script)
        self.assertLess(script.index(process_check), script.index("[1/5]"))
        skip_desktop = "PHOTOCOLLECT_SKIP_DESKTOP_DEPLOY"
        self.assertGreaterEqual(script.count(skip_desktop), 4)
        self.assertLess(script.index(skip_desktop), script.index(process_check))
        self.assertIn(":desktop_deploy_done", script)

    def test_release_pipeline_runs_pytest_style_test_modules(self):
        import ast

        tests_dir = Path(__file__).resolve().parent
        script = (tests_dir.parent / "一键构建_PhotoCollect.bat").read_text(
            encoding="utf-8"
        )
        # unittest discovery ignores module-level test functions, so every
        # such module must be passed to the gate's pytest step explicitly.
        function_style = sorted(
            path.name
            for path in tests_dir.glob("test*.py")
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")
                for node in ast.parse(path.read_text(encoding="utf-8")).body
            )
        )
        pytest_lines = [
            line for line in script.splitlines() if " -m pytest " in line
        ]

        self.assertIn("test_authorization_service.py", function_style)
        self.assertEqual(len(pytest_lines), 1)
        pytest_line = pytest_lines[0]
        for name in function_style:
            self.assertIn(f"tests\\{name}", pytest_line)
        position = script.index(pytest_line)
        self.assertLess(
            script.index("%PY% -m unittest discover -s tests -v"),
            position,
        )
        self.assertLess(position, script.index("[2/5]"))
        following = script[position + len(pytest_line):].split()
        self.assertEqual(following[:4], ["if", "errorlevel", "1", "goto"])
        self.assertEqual(following[4], ":fail_tests")

    def test_spec_excludes_foreign_icu_that_can_shadow_windows_qt_runtime(self):
        spec = (
            Path(__file__).resolve().parent.parent / "PhotoCollect.spec"
        ).read_text(encoding="utf-8")

        self.assertIn("is_foreign_icu_binary", spec)
        self.assertIn("name == 'icuuc.dll'", spec)
        self.assertIn("name.startswith('icudt')", spec)
        self.assertIn(
            "a.binaries = [entry for entry in a.binaries if not is_foreign_icu_binary(entry)]",
            spec,
        )

    def test_compat_build_launcher_cannot_close_silently(self):
        script = (
            Path(__file__).resolve().parent.parent / "一键发布_v515.bat"
        ).read_text(encoding="ascii")

        self.assertIn('set "PHOTOCOLLECT_VERSION_ONLY="', script)
        self.assertIn('set "PHOTOCOLLECT_NO_PAUSE=1"', script)
        self.assertIn('if not defined BUILD_SCRIPT', script)
        self.assertIn('call "%BUILD_SCRIPT%"', script)
        self.assertIn("pause", script)

    def test_synology_publisher_has_no_integrity_or_notification_bypass(self):
        root = Path(__file__).resolve().parent.parent
        publisher = (root / "publish_to_synology.ps1").read_text(encoding="utf-8")
        combined = (root / "一键构建并推送更新.bat").read_text(encoding="utf-8")

        self.assertNotIn("SkipHttpCheck", publisher)
        self.assertNotIn("SkipNotify", publisher)
        self.assertIn("ProtectedData]::Protect", publisher)
        self.assertIn("DataProtectionScope]::CurrentUser", publisher)
        self.assertIn("Send-ReleaseNotification", publisher)
        self.assertIn('"published_notified"', publisher)
        self.assertIn("[switch]$InitializeTokenOnly", publisher)
        self.assertLess(
            combined.index("一键发布到群晖.bat\" -InitializeTokenOnly"),
            combined.index("一键构建_PhotoCollect.bat"),
        )
        self.assertIn('一键发布到群晖.bat" -Yes', combined)
        self.assertIn("[version]$version", publisher)
        self.assertIn("[version]$existingLatest.version", publisher)
        self.assertIn("群晖上的 latest.json 版本更高，拒绝降级发布", publisher)
        self.assertIn("版本号未递增，请先修改 con.py 中的 APP_VERSION", publisher)

    def test_ocr_smoke_requires_exact_regression_result(self):
        correct = {
            "container_no": con.OCR_REGRESSION_CONTAINER_NO,
            "size_type": con.OCR_REGRESSION_SIZE_TYPE,
            "check_digit_valid": True,
            "elapsed_ms": 15,
        }
        wrong = {
            "container_no": "ADGZ3501837",
            "size_type": con.OCR_REGRESSION_SIZE_TYPE,
            "check_digit_valid": True,
            "elapsed_ms": 12,
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            correct_output = base / "correct.json"
            with patch.object(con, "run_ocr_image", return_value=correct):
                correct_exit = con.run_ocr_smoke_cli([
                    "con.py",
                    "--ocr-smoke",
                    str(self.fixture),
                    "--ocr-output",
                    str(correct_output),
                ])
            self.assertEqual(correct_exit, 0)
            self.assertTrue(json.loads(correct_output.read_text(encoding="utf-8"))["ok"])

            wrong_output = base / "wrong.json"
            with patch.object(con, "run_ocr_image", return_value=wrong):
                wrong_exit = con.run_ocr_smoke_cli([
                    "con.py",
                    "--ocr-smoke",
                    str(self.fixture),
                    "--ocr-output",
                    str(wrong_output),
                ])
            wrong_report = json.loads(wrong_output.read_text(encoding="utf-8"))
            self.assertEqual(wrong_exit, 1)
            self.assertFalse(wrong_report["ok"])
            self.assertEqual(wrong_report["container_no"], "ADGZ3501837")
            self.assertIn("OCR regression mismatch", wrong_report["error"])


if __name__ == "__main__":
    unittest.main()
