"""Run an isolated end-to-end smoke test of the packaged self-updater."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psutil

from updater_services import (
    DEFAULT_MANIFEST_URL,
    parse_update_manifest,
    prepare_install_plan,
    sha256_file,
)


def project_version(root: Path) -> str:
    source = (root / "con.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', source)
    if match is None:
        raise RuntimeError("Unable to read APP_VERSION from con.py")
    return match.group(1)


def matching_processes(executable: Path) -> list[psutil.Process]:
    expected = os.path.normcase(str(executable.resolve()))
    matches = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            actual = process.info.get("exe")
            if actual and os.path.normcase(str(Path(actual).resolve())) == expected:
                matches.append(process)
        except (OSError, psutil.Error):
            continue
    return matches


def stop_test_processes(executable: Path) -> None:
    for _attempt in range(3):
        matches = matching_processes(executable)
        if not matches:
            return
        for process in matches:
            try:
                process.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(matches, timeout=10)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass
        time.sleep(0.25)
    if matching_processes(executable):
        raise RuntimeError("Unable to stop isolated updater smoke process")


def run_helper(plan, update_dir: Path, environment: dict[str, str]):
    powershell = (
        Path(environment.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        alternative = shutil.which("pwsh.exe")
        if not alternative:
            raise RuntimeError("Windows PowerShell and PowerShell 7 are unavailable")
        powershell = Path(alternative)
    command = [
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
    ]
    return subprocess.run(
        command,
        cwd=str(update_dir),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_success_smoke(executable: Path, root: Path) -> dict:
    executable = executable.resolve(strict=True)
    version = project_version(root)
    source_size = executable.stat().st_size
    source_hash = sha256_file(executable)

    with tempfile.TemporaryDirectory(prefix="PhotoCollect-updater-e2e-") as tmp:
        sandbox = Path(tmp)
        local_appdata = sandbox / "LocalAppData"
        roaming_appdata = sandbox / "AppData"
        update_dir = local_appdata / "PhotoCollect" / "updates"
        update_dir.mkdir(parents=True)
        roaming_appdata.mkdir()

        target = sandbox / "PhotoCollect-e2e.exe"
        staged = update_dir / f"PhotoCollect-v{version}.exe"
        shutil.copy2(executable, target)
        shutil.copy2(executable, staged)

        manifest = parse_update_manifest(
            {
                "schema_version": 1,
                "app": "PhotoCollect",
                "channel": "stable",
                "version": version,
                "release_id": f"PhotoCollect:{version}:{source_hash}",
                "published_at": "2026-07-31T00:00:00+00:00",
                "notes": "isolated updater smoke",
                "file": {
                    "name": staged.name,
                    "url": (
                        f"http://192.168.110.79:18080/{staged.name}"
                    ),
                    "size_bytes": source_size,
                    "sha256": source_hash,
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
        )
        plan_data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
        log_path = Path(plan_data["log_path"])

        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(local_appdata)
        environment["APPDATA"] = str(roaming_appdata)
        environment["QT_QPA_PLATFORM"] = "offscreen"

        try:
            completed = run_helper(plan, update_dir, environment)
            log_text = (
                log_path.read_text(encoding="utf-8-sig")
                if log_path.is_file()
                else ""
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "Updater helper failed.\n"
                    + completed.stdout
                    + "\n"
                    + log_text
                )
            if sha256_file(target) != source_hash:
                raise RuntimeError("Installed target hash does not match staged EXE")
            if not plan.backup_path.is_file():
                raise RuntimeError("Updater did not retain the previous EXE backup")
            if sha256_file(plan.backup_path) != source_hash:
                raise RuntimeError("Rollback backup hash is invalid")
            if (
                plan.plan_path.exists()
                or plan.helper_path.exists()
                or staged.exists()
                or plan.health_path.exists()
            ):
                raise RuntimeError("Updater did not clean committed staging files")
            if "Update succeeded." not in log_text:
                raise RuntimeError("Updater did not record a healthy startup")
            return {
                "ok": True,
                "version": version,
                "sha256": source_hash,
                "size_bytes": source_size,
                "helper_log": log_text,
            }
        finally:
            stop_test_processes(target)


def run_rollback_smoke(executable: Path, root: Path) -> dict:
    executable = executable.resolve(strict=True)
    version = project_version(root)
    original_hash = sha256_file(executable)
    invalid_payload = b"intentionally invalid executable for rollback verification"
    invalid_hash = sha256_file_bytes(invalid_payload)

    with tempfile.TemporaryDirectory(prefix="PhotoCollect-updater-rollback-") as tmp:
        sandbox = Path(tmp)
        local_appdata = sandbox / "LocalAppData"
        roaming_appdata = sandbox / "AppData"
        update_dir = local_appdata / "PhotoCollect" / "updates"
        update_dir.mkdir(parents=True)
        roaming_appdata.mkdir()

        target = sandbox / "PhotoCollect-rollback.exe"
        staged = update_dir / f"PhotoCollect-v{version}.exe"
        shutil.copy2(executable, target)
        staged.write_bytes(invalid_payload)
        manifest = parse_update_manifest(
            {
                "schema_version": 1,
                "app": "PhotoCollect",
                "channel": "stable",
                "version": version,
                "release_id": f"PhotoCollect:{version}:{invalid_hash}",
                "published_at": "2026-07-31T00:00:00+00:00",
                "notes": "forced rollback smoke",
                "file": {
                    "name": staged.name,
                    "url": f"http://192.168.110.79:18080/{staged.name}",
                    "size_bytes": len(invalid_payload),
                    "sha256": invalid_hash,
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
        plan_data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
        log_path = Path(plan_data["log_path"])
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(local_appdata)
        environment["APPDATA"] = str(roaming_appdata)
        environment["QT_QPA_PLATFORM"] = "offscreen"

        try:
            completed = run_helper(plan, update_dir, environment)
            log_text = (
                log_path.read_text(encoding="utf-8-sig")
                if log_path.is_file()
                else ""
            )
            if completed.returncode == 0:
                raise RuntimeError("Forced-bad update unexpectedly succeeded")
            if sha256_file(target) != original_hash:
                raise RuntimeError("Rollback did not restore the original EXE")
            required_log_lines = (
                "Atomic replacement committed.",
                "Update failed:",
                "Rollback completed and previous version restarted.",
            )
            if any(line not in log_text for line in required_log_lines):
                raise RuntimeError("Rollback lifecycle was not fully recorded")
            if plan.plan_path.exists() or plan.helper_path.exists():
                raise RuntimeError("Failed update left executable hand-off scripts behind")
            return {
                "ok": True,
                "restored_sha256": original_hash,
                "helper_exit_code": completed.returncode,
                "helper_log": log_text,
            }
        finally:
            stop_test_processes(target)


def sha256_file_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def run_smoke(executable: Path, root: Path) -> dict:
    success = run_success_smoke(executable, root)
    rollback = run_rollback_smoke(executable, root)
    return {
        "ok": True,
        "version": success["version"],
        "sha256": success["sha256"],
        "size_bytes": success["size_bytes"],
        "healthy_update": success,
        "forced_rollback": rollback,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exe",
        type=Path,
        default=Path(__file__).resolve().parent / "dist" / "PhotoCollect.exe",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        result = run_smoke(args.exe, root)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
