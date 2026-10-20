"""Exercise the exact old-helper -> bridge -> real PhotoCollect transition."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from transition_bridge import sha256_file
from updater_services import (
    DEFAULT_MANIFEST_URL,
    parse_update_manifest,
    prepare_install_plan,
)


def project_version(root: Path) -> str:
    source = (root / "con.py").read_text(encoding="utf-8")
    match = re.search(
        r'(?m)^\s*APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        source,
    )
    if match is None:
        raise RuntimeError("Unable to read APP_VERSION from con.py")
    return match.group(1)


def stop_sandbox_processes(root: Path) -> None:
    expected = os.path.normcase(str(root.resolve()))
    for _attempt in range(3):
        matches: list[psutil.Process] = []
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
        if not matches:
            return
        _gone, alive = psutil.wait_procs(matches, timeout=5)
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=5)
        time.sleep(0.25)


def _read_log(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_transition_smoke(
    bridge_executable: Path,
    product_executable: Path,
    previous_executable: Path,
    *,
    root: Path,
) -> dict:
    version = project_version(root)
    bridge = Path(bridge_executable).resolve(strict=True)
    product = Path(product_executable).resolve(strict=True)
    previous = Path(previous_executable).resolve(strict=True)
    bridge_hash = sha256_file(bridge)
    product_hash = sha256_file(product)
    previous_hash = sha256_file(previous)
    started_at = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(
        prefix="PhotoCollect-production-transition-",
        ignore_cleanup_errors=True,
    ) as raw:
        sandbox = Path(raw)
        local_appdata = sandbox / "LocalAppData"
        roaming_appdata = sandbox / "AppData"
        update_dir = local_appdata / "PhotoCollect" / "updates"
        update_dir.mkdir(parents=True)
        roaming_appdata.mkdir(parents=True)
        target = sandbox / "PhotoCollect.exe"
        staged = update_dir / f"PhotoCollect-v{version}.exe"
        shutil.copy2(previous, target)
        shutil.copy2(bridge, staged)
        manifest = parse_update_manifest(
            {
                "schema_version": 1,
                "app": "PhotoCollect",
                "channel": "stable",
                "version": version,
                "release_id": f"PhotoCollect:{version}:{bridge_hash}",
                "published_at": started_at.isoformat(),
                "notes": "production transition bridge smoke",
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
            health_timeout_seconds=45,
        )
        helper_text = plan.helper_path.read_text(encoding="utf-8")
        reset_line = '$env:PYINSTALLER_RESET_ENVIRONMENT = "1"'
        if reset_line not in helper_text:
            raise RuntimeError("Current helper no longer exposes the transition seam")
        plan.helper_path.write_text(
            helper_text.replace(reset_line, "# reset flag absent in v5.16.1"),
            encoding="utf-8",
            newline="\n",
        )
        plan_data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
        install_log = Path(plan_data["log_path"])
        bridge_log = update_dir / f"transition-v{version}.log"
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = str(local_appdata)
        environment["APPDATA"] = str(roaming_appdata)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        powershell = (
            Path(environment.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
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

        try:
            helper = subprocess.run(
                command,
                cwd=str(update_dir),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=240,
                check=False,
            )
            if helper.returncode != 0:
                raise RuntimeError(
                    "Old helper rejected the production bridge.\n"
                    + _read_log(install_log)
                    + "\n"
                    + _read_log(bridge_log)
                )

            deadline = time.monotonic() + 300
            installed_hash = None
            while time.monotonic() < deadline:
                try:
                    installed_hash = sha256_file(target) if target.is_file() else None
                except OSError:
                    installed_hash = None
                if installed_hash == product_hash and (
                    "Transition succeeded." in _read_log(bridge_log)
                ):
                    break
                time.sleep(0.5)
            try:
                installed_hash = sha256_file(target) if target.is_file() else None
            except OSError:
                installed_hash = None
            if installed_hash != product_hash:
                raise RuntimeError(
                    "Bridge did not install the exact accepted product.\n"
                    + _read_log(install_log)
                    + "\n"
                    + _read_log(bridge_log)
                )
            if "Transition succeeded." not in _read_log(bridge_log):
                raise RuntimeError("Bridge never recorded final healthy startup")
            backup_retained = plan.backup_path.is_file()
            if backup_retained and sha256_file(plan.backup_path) != previous_hash:
                raise RuntimeError("Previous EXE backup hash changed during transition")
            completed_at = datetime.now(timezone.utc)
            return {
                "ok": True,
                "version": version,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_seconds": round(
                    (completed_at - started_at).total_seconds(),
                    3,
                ),
                "runtime": {
                    "executable": str(bridge),
                    "size_bytes": bridge.stat().st_size,
                    "sha256": bridge_hash,
                },
                "embedded_product": {
                    "executable": str(product),
                    "size_bytes": product.stat().st_size,
                    "sha256": product_hash,
                },
                "previous_version": {
                    "executable": str(previous),
                    "size_bytes": previous.stat().st_size,
                    "sha256": previous_hash,
                },
                "old_helper_reset_flag_removed": True,
                "final_target_sha256": installed_hash,
                "rollback_backup_sha256": previous_hash,
                "rollback_backup_retained_after_health": backup_retained,
                "helper_log": _read_log(install_log),
                "bridge_log": _read_log(bridge_log),
            }
        finally:
            stop_sandbox_processes(sandbox)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        report = run_transition_smoke(
            args.bridge,
            args.product,
            args.previous,
            root=root,
        )
        _atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
