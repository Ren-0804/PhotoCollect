import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MANIFEST_URL = "http://192.168.110.79:18080/latest.json"


def valid_manifest(**overrides):
    payload = {
        "schema_version": 1,
        "app": "PhotoCollect",
        "channel": "stable",
        "version": "5.16.0",
        "release_id": "PhotoCollect:5.16.0:" + ("a" * 64),
        "published_at": "2026-07-31T08:20:13+00:00",
        "notes": "软件内更新首版",
        "file": {
            "name": "PhotoCollect-v5.16.0.exe",
            "url": "http://192.168.110.79:18080/PhotoCollect-v5.16.0.exe",
            "size_bytes": 134_724_107,
            "sha256": "a" * 64,
        },
    }
    for key, value in overrides.items():
        if key.startswith("file__"):
            payload["file"][key.removeprefix("file__")] = value
        else:
            payload[key] = value
    return payload


def manifest_for_payload(payload: bytes):
    digest = hashlib.sha256(payload).hexdigest()
    return valid_manifest(
        release_id=f"PhotoCollect:5.16.0:{digest}",
        file__size_bytes=len(payload),
        file__sha256=digest,
    )


class _FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str, *, status: int = 200, headers=None):
        super().__init__(payload)
        self.status = status
        self._url = url
        self.headers = headers or {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return self.response


class UpdateManifestTests(unittest.TestCase):
    def test_valid_manifest_reports_a_newer_release(self):
        from updater_services import parse_update_manifest

        manifest = parse_update_manifest(
            valid_manifest(),
            manifest_url=MANIFEST_URL,
        )

        self.assertEqual(manifest.version, "5.16.0")
        self.assertTrue(manifest.is_newer_than("5.15.2"))

    def test_external_download_origin_is_rejected(self):
        from updater_services import ManifestError, parse_update_manifest

        payload = valid_manifest(
            file__url="http://attacker.invalid/PhotoCollect-v5.16.0.exe"
        )

        with self.assertRaisesRegex(ManifestError, "同一服务器"):
            parse_update_manifest(payload, manifest_url=MANIFEST_URL)

    def test_release_id_must_bind_version_and_hash(self):
        from updater_services import ManifestError, parse_update_manifest

        with self.assertRaisesRegex(ManifestError, "release_id"):
            parse_update_manifest(
                valid_manifest(release_id="PhotoCollect:5.16.0:" + ("b" * 64)),
                manifest_url=MANIFEST_URL,
            )

    def test_equal_or_older_version_is_not_newer(self):
        from updater_services import is_newer_version

        self.assertFalse(is_newer_version("5.15.2", "5.15.2"))
        self.assertFalse(is_newer_version("5.15.1", "5.15.2"))


class UpdateCheckTests(unittest.TestCase):
    def test_check_fetches_latest_json_without_blocking_ui_code(self):
        from updater_services import check_for_update

        body = json.dumps(valid_manifest()).encode("utf-8")
        opener = _FakeOpener(_FakeResponse(body, MANIFEST_URL))

        result = check_for_update(
            "5.15.2",
            manifest_url=MANIFEST_URL,
            opener=opener,
        )

        self.assertTrue(result.available)
        self.assertEqual(result.manifest.version, "5.16.0")
        self.assertEqual(opener.requests[0][0].full_url, MANIFEST_URL)
        self.assertEqual(opener.requests[0][0].get_header("Cache-control"), "no-cache")


class UpdateDownloadTests(unittest.TestCase):
    def test_download_streams_to_a_verified_staging_file(self):
        from updater_services import download_update, parse_update_manifest

        payload = (b"PhotoCollect-update-payload-" * 512) + b"done"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )
        response = _FakeResponse(
            payload,
            manifest.file_url,
            headers={"Content-Length": str(len(payload))},
        )
        progress = []

        with tempfile.TemporaryDirectory() as tmp:
            result = download_update(
                manifest,
                destination_dir=Path(tmp),
                opener=_FakeOpener(response),
                progress_callback=lambda current, total: progress.append(
                    (current, total)
                ),
            )

            self.assertEqual(result.read_bytes(), payload)
            self.assertEqual(progress[-1], (len(payload), len(payload)))
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_hash_mismatch_leaves_no_installable_or_partial_file(self):
        from updater_services import (
            DownloadError,
            download_update,
            parse_update_manifest,
        )

        payload = b"tampered"
        manifest = parse_update_manifest(
            valid_manifest(
                release_id="PhotoCollect:5.16.0:" + ("b" * 64),
                file__size_bytes=len(payload),
                file__sha256="b" * 64,
            ),
            manifest_url=MANIFEST_URL,
        )

        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            with self.assertRaisesRegex(DownloadError, "SHA-256"):
                download_update(
                    manifest,
                    destination_dir=target_dir,
                    opener=_FakeOpener(
                        _FakeResponse(
                            payload,
                            manifest.file_url,
                            headers={"Content-Length": str(len(payload))},
                        )
                    ),
                )

            self.assertEqual(list(target_dir.iterdir()), [])

    def test_cancelled_download_removes_the_partial_file(self):
        from updater_services import (
            DownloadCancelled,
            download_update,
            parse_update_manifest,
        )

        payload = b"pending update"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )
        cancelled = threading.Event()
        cancelled.set()

        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            with self.assertRaises(DownloadCancelled):
                download_update(
                    manifest,
                    destination_dir=target_dir,
                    opener=_FakeOpener(_FakeResponse(payload, manifest.file_url)),
                    cancel_event=cancelled,
                )

            self.assertEqual(list(target_dir.iterdir()), [])


class UpdateInstallPlanTests(unittest.TestCase):
    def test_prepare_install_plan_binds_verified_payload_and_rollback_paths(self):
        from updater_services import (
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"verified one-file executable"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old executable")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)

            plan = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                current_pid=43210,
                update_dir=update_dir,
            )
            plan_data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
            helper_text = plan.helper_path.read_text(encoding="utf-8")

            self.assertEqual(Path(plan_data["target_path"]), target.resolve())
            self.assertEqual(Path(plan_data["staged_path"]), staged.resolve())
            self.assertEqual(plan_data["expected_sha256"], manifest.sha256)
            self.assertEqual(plan_data["expected_size"], len(payload))
            self.assertEqual(plan_data["wait_pid"], 43210)
            self.assertEqual(
                Path(plan_data["wait_process_path"]),
                target.resolve(),
            )
            self.assertEqual(plan_data["health_timeout_seconds"], 45)
            self.assertEqual(
                Path(plan_data["failure_path"]),
                update_dir.resolve() / "last-install-failure.json",
            )
            self.assertEqual(
                Path(plan_data["replacement_path"]).parent,
                target.resolve().parent,
            )
            self.assertEqual(
                Path(plan_data["backup_path"]).parent,
                target.resolve().parent,
            )
            self.assertIn("[System.IO.File]::Replace", helper_text)
            self.assertIn("health_path", helper_text)
            self.assertIn("Rollback", helper_text)
            self.assertIn("Write-UpdateFailure", helper_text)
            self.assertIn("Security.Cryptography.SHA256", helper_text)
            self.assertNotIn("Get-FileHash", helper_text)
            self.assertIn("wait_process_path", helper_text)
            self.assertIn("PhotoCollectSelfUpdate", helper_text)
            self.assertIn("Get-TargetProcessIds", helper_text)
            self.assertIn("Wait-TargetProcesses -Target $target", helper_text)
            self.assertIn("Invoke-FileReplaceWithRetry -Source $replacement", helper_text)
            self.assertIn("Invoke-FileReplaceWithRetry -Source $Backup", helper_text)
            self.assertEqual(helper_text.count("[System.IO.File]::Replace("), 1)
            reset_environment = (
                '$env:PYINSTALLER_RESET_ENVIRONMENT = "1"'
            )
            self.assertIn(reset_environment, helper_text)
            self.assertLess(
                helper_text.index(reset_environment),
                helper_text.index("Start-Process -FilePath $target"),
            )

    def test_each_install_plan_has_an_independent_helper(self):
        from updater_services import (
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"verified one-file executable"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old executable")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)

            first = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                current_pid=43210,
                update_dir=update_dir,
            )
            second = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                current_pid=43210,
                update_dir=update_dir,
            )

            self.assertNotEqual(first.plan_path, second.plan_path)
            self.assertNotEqual(first.helper_path, second.helper_path)
            self.assertTrue(first.helper_path.is_file())
            self.assertTrue(second.helper_path.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell helper")
    def test_generated_helper_parses_as_powershell(self):
        from updater_services import (
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"verified"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)
            plan = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                update_dir=update_dir,
            )
            environment = os.environ.copy()
            environment["PHOTOCOLLECT_HELPER_TEST"] = str(plan.helper_path)
            powershell = (
                Path(environment.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if not powershell.is_file():
                alternative = shutil.which("pwsh")
                if not alternative:
                    self.skipTest("PowerShell is unavailable")
                powershell = Path(alternative)

            completed = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "[scriptblock]::Create("
                        "[IO.File]::ReadAllText("
                        "$env:PHOTOCOLLECT_HELPER_TEST)) | Out-Null"
                    ),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_helper_retries_a_temporarily_locked_target(self):
        import ctypes
        import time
        from ctypes import wintypes

        from updater_services import prepare_install_plan, parse_update_manifest

        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            alternative = shutil.which("pwsh.exe")
            if not alternative:
                self.skipTest("PowerShell is unavailable")
            powershell = Path(alternative)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        invalid_handle = ctypes.c_void_p(-1).value

        payload = b"new executable, intentionally invalid for rollback"
        manifest = parse_update_manifest(
            manifest_for_payload(payload), manifest_url=MANIFEST_URL,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old executable")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)
            plan = prepare_install_plan(
                manifest, staged, target_executable=target,
                current_pid=2_000_000_000, update_dir=update_dir,
            )
            log_path = update_dir / f"install-v{manifest.version}.log"

            def diagnostics(output):
                log_text = (
                    log_path.read_text(encoding="utf-8-sig")
                    if log_path.is_file()
                    else "<no install log>"
                )
                return (
                    f"\nexit code: {process.returncode}"
                    f"\n--- helper output ---\n{output}"
                    f"\n--- install log ---\n{log_text}"
                )

            handle = kernel32.CreateFileW(
                str(target), 0x80000000, 0, None, 3, 0x80, None,
            )
            self.assertNotEqual(handle, invalid_handle)
            process = None
            try:
                process = subprocess.Popen(
                    [
                        str(powershell), "-NoLogo", "-NoProfile",
                        "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(plan.helper_path),
                        "-PlanPath", str(plan.plan_path),
                    ],
                    cwd=str(update_dir), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                # Normally ~1 s; a loaded machine has stalled PowerShell
                # startup for ~13 s, so these limits only guard against hangs.
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if log_path.is_file() and "File replacement is blocked" in log_path.read_text(encoding="utf-8-sig"):
                        outcome = "blocked"
                        break
                    if process.poll() is not None:
                        outcome = "exited"
                        break
                    time.sleep(0.1)
                else:
                    outcome = "timeout"
            finally:
                kernel32.CloseHandle(handle)
                if process is not None:
                    try:
                        output, _ = process.communicate(timeout=60)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        output, _ = process.communicate(timeout=5)
                        self.fail(
                            "Updater did not finish after the target lock was released"
                            + diagnostics(output)
                        )

            self.assertNotEqual(
                outcome,
                "exited",
                "Updater exited before reaching the locked target" + diagnostics(output),
            )
            self.assertNotEqual(
                outcome,
                "timeout",
                "Updater did not report the locked target" + diagnostics(output),
            )
            log_text = log_path.read_text(encoding="utf-8-sig")
            self.assertNotEqual(process.returncode, 0, output + log_text)
            self.assertIn("File replacement succeeded after", log_text)
            self.assertIn("Rollback completed", log_text)
            self.assertEqual(target.read_bytes(), b"old executable")

    def test_prepare_install_plan_rejects_a_tampered_staging_file(self):
        from updater_services import (
            DownloadError,
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"expected"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(b"tampered")

            with self.assertRaises(DownloadError):
                prepare_install_plan(
                    manifest,
                    staged,
                    target_executable=target,
                    update_dir=update_dir,
                )

    def test_post_update_health_arguments_are_consumed_and_safely_written(self):
        from updater_services import (
            consume_post_update_health_args,
            write_post_update_health,
        )

        token = "1" * 32
        with tempfile.TemporaryDirectory() as tmp:
            update_dir = Path(tmp)
            marker = update_dir / f"health-{token}.json"
            argv = [
                "PhotoCollect.exe",
                "--post-update-health",
                str(marker),
                "--post-update-token",
                token,
                "--ordinary-argument",
            ]

            health = consume_post_update_health_args(argv)
            self.assertEqual(health, (marker, token))
            self.assertEqual(argv, ["PhotoCollect.exe", "--ordinary-argument"])

            write_post_update_health(
                marker,
                token,
                "5.16.0",
                allowed_dir=update_dir,
            )
            saved = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(saved["token"], token)
            self.assertEqual(saved["version"], "5.16.0")

    def test_successful_update_backup_is_removed_only_by_explicit_cleanup(self):
        from updater_services import cleanup_successful_update_backup

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "PhotoCollect.exe"
            backup = target.with_name(target.name + ".previous")
            target.write_bytes(b"new")
            backup.write_bytes(b"old")

            self.assertTrue(cleanup_successful_update_backup(target))
            self.assertFalse(backup.exists())
            self.assertTrue(target.exists())

    def test_update_failure_is_consumed_once(self):
        from updater_services import consume_update_failure

        with tempfile.TemporaryDirectory() as tmp:
            update_dir = Path(tmp)
            marker = update_dir / "last-install-failure.json"
            marker.write_text(
                json.dumps({
                    "schema_version": 1,
                    "app": "PhotoCollect",
                    "status": "failed",
                    "version": "5.16.11",
                    "message": "health timeout",
                    "log_path": str(update_dir / "install.log"),
                }),
                encoding="utf-8",
            )

            result = consume_update_failure(update_dir=update_dir)

            self.assertEqual(result["version"], "5.16.11")
            self.assertEqual(result["message"], "health timeout")
            self.assertIsNone(result["recovered"])
            self.assertFalse(marker.exists())
            self.assertIsNone(consume_update_failure(update_dir=update_dir))

    def test_update_failure_preserves_explicit_recovery_status(self):
        from updater_services import consume_update_failure

        with tempfile.TemporaryDirectory() as tmp:
            update_dir = Path(tmp)
            marker = update_dir / "last-install-failure.json"
            for recovered in (True, False):
                marker.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "app": "PhotoCollect",
                        "status": "failed",
                        "version": "5.16.15",
                        "message": "replacement failed",
                        "recovered": recovered,
                        "log_path": str(update_dir / "install.log"),
                    }),
                    encoding="utf-8",
                )
                result = consume_update_failure(update_dir=update_dir)
                self.assertIs(result["recovered"], recovered)

    def test_launch_helper_uses_hidden_detached_powershell_process(self):
        from updater_services import (
            launch_install_helper,
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"verified"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)
            plan = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                update_dir=update_dir,
            )
            process = object()
            popen = Mock(return_value=process)

            result = launch_install_helper(
                plan,
                popen_factory=popen,
                powershell_executable="powershell.exe",
            )

            self.assertIs(result, process)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "powershell.exe")
            self.assertIn("-NoProfile", command)
            self.assertIn(str(plan.helper_path), command)
            self.assertIn(str(plan.plan_path), command)
            self.assertNotEqual(popen.call_args.kwargs["creationflags"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows update helper")
    def test_launch_helper_falls_back_to_powershell_7(self):
        from updater_services import (
            launch_install_helper,
            prepare_install_plan,
            parse_update_manifest,
        )

        payload = b"verified"
        manifest = parse_update_manifest(
            manifest_for_payload(payload),
            manifest_url=MANIFEST_URL,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "PhotoCollect.exe"
            target.write_bytes(b"old")
            update_dir = root / "updates"
            update_dir.mkdir()
            staged = update_dir / manifest.file_name
            staged.write_bytes(payload)
            plan = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                update_dir=update_dir,
            )
            pwsh = root / "Program Files" / "PowerShell" / "7" / "pwsh.exe"
            pwsh.parent.mkdir(parents=True)
            pwsh.write_bytes(b"fixture")
            popen = Mock(return_value=object())
            with patch.dict(
                os.environ,
                {"SystemRoot": str(root / "Windows"), "ProgramFiles": str(root / "Program Files")},
            ):
                launch_install_helper(plan, popen_factory=popen)

            self.assertEqual(popen.call_args.args[0][0], str(pwsh))


class UpdateNotificationTests(unittest.TestCase):
    def test_release_ready_ntfy_message_is_strictly_validated(self):
        from updater_services import parse_ntfy_release_event

        release = valid_manifest()
        notification = {
            "type": "release.ready",
            "release_id": release["release_id"],
            "version": release["version"],
            "manifest_url": MANIFEST_URL,
            "sha256": release["file"]["sha256"],
            "size_bytes": release["file"]["size_bytes"],
        }
        envelope = {
            "id": "abc",
            "event": "message",
            "topic": "photocollect-updates",
            "message": json.dumps(notification),
        }

        result = parse_ntfy_release_event(
            json.dumps(envelope).encode("utf-8"),
            expected_manifest_url=MANIFEST_URL,
        )

        self.assertEqual(result["version"], "5.16.0")
        self.assertEqual(result["release_id"], release["release_id"])

    def test_ntfy_message_cannot_redirect_checks_to_another_server(self):
        from updater_services import parse_ntfy_release_event

        release = valid_manifest()
        notification = {
            "type": "release.ready",
            "release_id": release["release_id"],
            "version": release["version"],
            "manifest_url": "http://attacker.invalid/latest.json",
            "sha256": release["file"]["sha256"],
            "size_bytes": release["file"]["size_bytes"],
        }
        envelope = {
            "event": "message",
            "topic": "photocollect-updates",
            "message": json.dumps(notification),
        }

        self.assertIsNone(
            parse_ntfy_release_event(
                json.dumps(envelope).encode("utf-8"),
                expected_manifest_url=MANIFEST_URL,
            )
        )

if __name__ == "__main__":
    unittest.main()
