import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from updater_services import (
    DEFAULT_MANIFEST_URL,
    parse_update_manifest,
    prepare_install_plan,
)


CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


def stop_fixture_processes(root: Path) -> None:
    expected = os.path.normcase(str(root.resolve()))
    matches = []
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            executable = process.info.get("exe") or ""
            command = " ".join(process.info.get("cmdline") or [])
            if (
                os.path.normcase(executable).startswith(expected + os.sep)
                or expected in os.path.normcase(command)
            ):
                matches.append(process)
                process.terminate()
        except (OSError, psutil.Error):
            continue
    _gone, alive = psutil.wait_procs(matches, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass


def compile_health_fixture(
    csc: Path,
    output: Path,
    *,
    version: str,
    require_reset_environment: bool,
    write_health: bool = True,
) -> None:
    source = output.with_suffix(".cs")
    required = "true" if require_reset_environment else "false"
    writes_health = "true" if write_health else "false"
    source.write_text(
        r'''using System;
using System.IO;
using System.Text;
using System.Threading;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string marker = null;
        string token = null;
        for (int index = 0; index + 1 < args.Length; index += 2)
        {
            if (args[index] == "--post-update-health") marker = args[index + 1];
            if (args[index] == "--post-update-token") token = args[index + 1];
        }
        if (__REQUIRE_RESET__ &&
            Environment.GetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT") != "1")
            return 9;
        if (__WRITE_HEALTH__ &&
            !String.IsNullOrEmpty(marker) && !String.IsNullOrEmpty(token))
        {
            string json = "{\"schema_version\":1,\"app\":\"PhotoCollect\","
                + "\"version\":\"__VERSION__\",\"token\":\"" + token
                + "\",\"pid\":" + System.Diagnostics.Process.GetCurrentProcess().Id + "}";
            string temporary = marker + ".tmp";
            File.WriteAllText(temporary, json, new UTF8Encoding(false));
            if (File.Exists(marker)) File.Delete(marker);
            File.Move(temporary, marker);
        }
        Thread.Sleep(3000);
        return 0;
    }
}
'''.replace("__REQUIRE_RESET__", required)
        .replace("__WRITE_HEALTH__", writes_health)
        .replace("__VERSION__", version),
        encoding="utf-8",
        newline="\n",
    )
    completed = subprocess.run(
        [
            str(csc),
            "/nologo",
            "/target:winexe",
            "/platform:x64",
            f"/out:{output}",
            str(source),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout)


class TransitionBridgeBuilderTests(unittest.TestCase):
    def test_bridge_templates_never_delete_before_committing_state(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "transition_bridge_bootstrap.cs",
            "transition_bridge_worker.cs",
        ):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("File.Delete(path);\n            File.Move(temporary, path);", source)
            self.assertIn("File.Replace(temporary, path, null, true);", source)
        worker_source = (root / "transition_bridge_worker.cs").read_text(encoding="utf-8")
        self.assertIn("started.Dispose();", worker_source)

    def test_built_bridge_extracts_the_exact_embedded_payload(self):
        from transition_bridge import build_transition_bridge, sha256_file

        csc = CSC
        if not csc.is_file():
            self.skipTest("64-bit .NET Framework C# compiler is unavailable")

        with tempfile.TemporaryDirectory(prefix="PhotoCollect-bridge-build-") as raw:
            root = Path(raw)
            payload = root / "real-PhotoCollect.exe"
            payload.write_bytes(b"MZ\x00PhotoCollect transition payload fixture\x00")
            bridge = root / "PhotoCollect.exe"
            extracted = root / "extracted.exe"

            result = build_transition_bridge(
                payload,
                bridge,
                version="5.16.2",
                csc_path=csc,
            )

            self.assertTrue(result.output_path.is_file())
            self.assertEqual(result.payload_size, payload.stat().st_size)
            self.assertEqual(result.payload_sha256, sha256_file(payload))
            completed = subprocess.run(
                [
                    str(result.output_path),
                    "--bridge-verify-payload",
                    str(extracted),
                ],
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(extracted.read_bytes(), payload.read_bytes())

    def test_bridge_normal_launch_resumes_an_interrupted_transition(self):
        from transition_bridge import build_transition_bridge, sha256_file

        if not CSC.is_file():
            self.skipTest("64-bit .NET Framework C# compiler is unavailable")

        with tempfile.TemporaryDirectory(
            prefix="PhotoCollect-bridge-resume-",
            ignore_cleanup_errors=True,
        ) as raw:
            root = Path(raw)
            self.addCleanup(stop_fixture_processes, root)
            local_appdata = root / "LocalAppData"
            update_dir = local_appdata / "PhotoCollect" / "updates"
            update_dir.mkdir(parents=True)
            target = root / "PhotoCollect.exe"
            real = root / "real-PhotoCollect.exe"
            compile_health_fixture(
                CSC,
                target,
                version="5.16.1",
                require_reset_environment=False,
            )
            compile_health_fixture(
                CSC,
                real,
                version="5.16.2",
                require_reset_environment=True,
            )
            previous = target.with_name(target.name + ".previous")
            shutil.copy2(target, previous)
            real_hash = sha256_file(real)
            build_transition_bridge(
                real,
                target,
                version="5.16.2",
                csc_path=CSC,
            )
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_appdata)
            environment["APPDATA"] = str(root / "AppData")
            process = subprocess.Popen(
                [str(target)],
                env=environment,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process.wait(timeout=60)

            deadline = time.monotonic() + 120
            bridge_log = update_dir / "transition-v5.16.2.log"
            while time.monotonic() < deadline:
                if (
                    target.is_file()
                    and sha256_file(target) == real_hash
                    and bridge_log.is_file()
                    and "Transition succeeded." in bridge_log.read_text(
                        encoding="utf-8-sig"
                    )
                ):
                    break
                time.sleep(0.25)
            self.assertEqual(
                sha256_file(target),
                real_hash,
                bridge_log.read_text(encoding="utf-8-sig")
                if bridge_log.exists()
                else "bridge log missing",
            )

    def test_old_helper_transitions_through_bridge_to_real_executable(self):
        from transition_bridge import build_transition_bridge, sha256_file

        if not CSC.is_file():
            self.skipTest("64-bit .NET Framework C# compiler is unavailable")

        with tempfile.TemporaryDirectory(
            prefix="PhotoCollect-bridge-e2e-",
            ignore_cleanup_errors=True,
        ) as raw:
            root = Path(raw)
            self.addCleanup(stop_fixture_processes, root)
            local_appdata = root / "LocalAppData"
            update_dir = local_appdata / "PhotoCollect" / "updates"
            update_dir.mkdir(parents=True)
            target = root / "PhotoCollect.exe"
            real = root / "real-PhotoCollect.exe"
            compile_health_fixture(
                CSC,
                target,
                version="5.16.1",
                require_reset_environment=False,
            )
            compile_health_fixture(
                CSC,
                real,
                version="5.16.2",
                require_reset_environment=True,
            )
            old_hash = sha256_file(target)
            real_hash = sha256_file(real)
            staged = update_dir / "PhotoCollect-v5.16.2.exe"
            build_transition_bridge(
                real,
                staged,
                version="5.16.2",
                csc_path=CSC,
            )
            bridge_hash = sha256_file(staged)
            manifest = parse_update_manifest(
                {
                    "schema_version": 1,
                    "app": "PhotoCollect",
                    "channel": "stable",
                    "version": "5.16.2",
                    "release_id": f"PhotoCollect:5.16.2:{bridge_hash}",
                    "published_at": "2026-07-31T00:00:00+00:00",
                    "notes": "transition bridge integration test",
                    "file": {
                        "name": staged.name,
                        "url": f"http://192.168.110.79:18080/{staged.name}",
                        "size_bytes": staged.stat().st_size,
                        "sha256": bridge_hash,
                    },
                },
                manifest_url=DEFAULT_MANIFEST_URL,
            )
            plan = prepare_install_plan(
                manifest,
                staged,
                target_executable=target,
                current_pid=2_000_000_000,
                update_dir=update_dir,
                health_timeout_seconds=5,
            )
            old_helper = plan.helper_path.read_text(encoding="utf-8").replace(
                '$env:PYINSTALLER_RESET_ENVIRONMENT = "1"',
                "# reset flag absent in v5.16.1",
            )
            plan.helper_path.write_text(old_helper, encoding="utf-8", newline="\n")
            plan_data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
            install_log = Path(plan_data["log_path"])
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_appdata)
            environment["APPDATA"] = str(root / "AppData")
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
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(plan.helper_path),
                    "-PlanPath",
                    str(plan.plan_path),
                ],
                cwd=str(update_dir),
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            self.assertEqual(
                completed.returncode,
                0,
                (
                    install_log.read_text(encoding="utf-8-sig")
                    if install_log.exists()
                    else ""
                )
                + "\n"
                + (
                    (update_dir / "transition-v5.16.2.log").read_text(
                        encoding="utf-8-sig"
                    )
                    if (update_dir / "transition-v5.16.2.log").exists()
                    else ""
                ),
            )

            deadline = time.monotonic() + 120
            bridge_log = update_dir / "transition-v5.16.2.log"
            while time.monotonic() < deadline:
                if (
                    target.is_file()
                    and sha256_file(target) == real_hash
                    and bridge_log.is_file()
                    and "Transition succeeded." in bridge_log.read_text(
                        encoding="utf-8-sig"
                    )
                ):
                    break
                time.sleep(0.25)

            self.assertEqual(sha256_file(target), real_hash)
            self.assertTrue(plan.backup_path.is_file())
            self.assertEqual(sha256_file(plan.backup_path), old_hash)
            self.assertIn(
                "Transition succeeded.",
                bridge_log.read_text(encoding="utf-8-sig"),
            )

    def test_unhealthy_real_executable_restores_previous_version(self):
        from transition_bridge import build_transition_bridge, sha256_file

        if not CSC.is_file():
            self.skipTest("64-bit .NET Framework C# compiler is unavailable")

        with tempfile.TemporaryDirectory(
            prefix="PhotoCollect-bridge-rollback-",
            ignore_cleanup_errors=True,
        ) as raw:
            root = Path(raw)
            self.addCleanup(stop_fixture_processes, root)
            local_appdata = root / "LocalAppData"
            update_dir = local_appdata / "PhotoCollect" / "updates"
            update_dir.mkdir(parents=True)
            target = root / "PhotoCollect.exe"
            unhealthy = root / "unhealthy-PhotoCollect.exe"
            compile_health_fixture(
                CSC,
                target,
                version="5.16.1",
                require_reset_environment=False,
            )
            compile_health_fixture(
                CSC,
                unhealthy,
                version="5.16.2",
                require_reset_environment=True,
                write_health=False,
            )
            previous = target.with_name(target.name + ".previous")
            shutil.copy2(target, previous)
            old_hash = sha256_file(target)
            build_transition_bridge(
                unhealthy,
                target,
                version="5.16.2",
                csc_path=CSC,
                health_timeout_seconds=5,
            )
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_appdata)
            environment["APPDATA"] = str(root / "AppData")
            bootstrap = subprocess.Popen(
                [str(target)],
                env=environment,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            deadline = time.monotonic() + 120
            bridge_log = update_dir / "transition-v5.16.2.log"
            while time.monotonic() < deadline:
                if (
                    target.is_file()
                    and sha256_file(target) == old_hash
                    and bridge_log.is_file()
                    and "Rollback completed" in bridge_log.read_text(
                        encoding="utf-8-sig"
                    )
                ):
                    break
                time.sleep(0.25)
            self.assertEqual(
                sha256_file(target),
                old_hash,
                bridge_log.read_text(encoding="utf-8-sig")
                if bridge_log.exists()
                else "bridge log missing",
            )
            self.assertIn(
                "Rollback completed",
                bridge_log.read_text(encoding="utf-8-sig"),
            )
            self.assertNotIn(
                "Rollback failed",
                bridge_log.read_text(encoding="utf-8-sig"),
            )
            bootstrap.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
