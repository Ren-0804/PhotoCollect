"""Secure, UI-agnostic update primitives for the PhotoCollect desktop app."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)


DEFAULT_MANIFEST_URL = "http://192.168.110.79:18080/latest.json"
DEFAULT_NTFY_URL = "http://192.168.110.79:18081/photocollect-updates/json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_UPDATE_BYTES = 512 * 1024 * 1024
MAX_NTFY_EVENT_BYTES = 16 * 1024
NTFY_TOPIC = "photocollect-updates"

_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_HEALTH_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


WINDOWS_UPDATE_HELPER = r"""param(
    [Parameter(Mandatory = $true)]
    [string]$PlanPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# PyInstaller 6.9+ treats a same-path restart as a worker process unless this
# public flag is set. Without it, the new one-file EXE reuses the old _MEI
# directory, which disappears as soon as the previous application exits.
$env:PYINSTALLER_RESET_ENVIRONMENT = "1"

$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$logPath = [string]$plan.log_path
$failurePath = [System.IO.Path]::GetFullPath([string]$plan.failure_path)

function Write-UpdateLog {
    param([string]$Message)
    # Logging is best effort. Add-Content denies concurrent readers and throws
    # under "Stop" whenever anything else has the log open, which used to abort
    # installs, rollbacks and the failure report. Append with read sharing and
    # drop the line if the file stays unavailable.
    $stamp = [DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
    $line = [System.Text.Encoding]::UTF8.GetBytes("$stamp $Message`r`n")
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            $stream = [System.IO.FileStream]::new(
                $logPath,
                [System.IO.FileMode]::Append,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]"ReadWrite, Delete"
            )
            try {
                if ($stream.Length -eq 0) {
                    $bom = [System.Text.Encoding]::UTF8.GetPreamble()
                    $stream.Write($bom, 0, $bom.Length)
                }
                $stream.Write($line, 0, $line.Length)
            }
            finally {
                $stream.Dispose()
            }
            return
        }
        catch {
            $cause = $_.Exception
            while ($null -ne $cause.InnerException) {
                $cause = $cause.InnerException
            }
            if (
                $cause -isnot [System.IO.IOException] -or
                $cause -is [System.IO.DirectoryNotFoundException]
            ) {
                return
            }
            Start-Sleep -Milliseconds 50
        }
    }
}

function Write-UpdateFailure {
    param(
        [string]$Message,
        [bool]$Recovered = $false
    )
    try {
        $failureData = @{
            schema_version = 1
            app = "PhotoCollect"
            status = "failed"
            version = [string]$plan.version
            message = $Message
            recovered = $Recovered
            log_path = $logPath
            failed_at = [DateTime]::UtcNow.ToString("o")
        } | ConvertTo-Json
        $failureTemp = "$failurePath.$([Guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::WriteAllText(
            $failureTemp,
            $failureData,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $failureTemp -Destination $failurePath -Force
    }
    catch {
        Write-UpdateLog "Unable to save update failure result: $($_.Exception.Message)"
    }
}

function Get-TargetProcessIds {
    param([string]$Target)
    $name = [System.IO.Path]::GetFileNameWithoutExtension($Target)
    $ids = @()
    foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
        try {
            $processPath = [System.IO.Path]::GetFullPath([string]$process.Path)
        }
        catch {
            continue
        }
        if ([string]::Equals($processPath, $Target, [StringComparison]::OrdinalIgnoreCase)) {
            $ids += [int]$process.Id
        }
    }
    return $ids
}

function Wait-TargetProcesses {
    param(
        [string]$Target,
        [int]$TimeoutSeconds
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $reported = $false
    while ($true) {
        $ids = @(Get-TargetProcessIds -Target $Target)
        if ($ids.Count -eq 0) {
            if ($reported) {
                Write-UpdateLog "All previous PhotoCollect processes exited."
            }
            return
        }
        if (-not $reported) {
            Write-UpdateLog "Waiting for executable processes to exit: $($ids -join ', ')."
            $reported = $true
        }
        if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            throw "PhotoCollect is still running (PID $($ids -join ', ')); close all windows and retry."
        }
        Start-Sleep -Milliseconds 250
    }
}

function Invoke-FileReplaceWithRetry {
    param(
        [string]$Source,
        [string]$Target,
        [string]$Backup,
        [int]$TimeoutSeconds = 30
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $attempts = 0
    while ($true) {
        $attempts++
        try {
            [System.IO.File]::Replace($Source, $Target, $Backup, $true)
            if ($attempts -gt 1) {
                Write-UpdateLog "File replacement succeeded after $attempts attempts."
            }
            return
        }
        catch {
            $cause = $_.Exception
            while ($null -ne $cause.InnerException) {
                $cause = $cause.InnerException
            }
            if (
                $cause -isnot [System.IO.IOException] -and
                $cause -isnot [System.UnauthorizedAccessException]
            ) {
                throw
            }
            if ($attempts -eq 1) {
                Write-UpdateLog "File replacement is blocked; retrying for up to $TimeoutSeconds seconds: $($cause.Message)"
            }
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                throw "Cannot replace $Target after $attempts attempts in $TimeoutSeconds seconds: $($cause.Message)"
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Assert-UpdateFile {
    param(
        [string]$Path,
        [Int64]$ExpectedSize,
        [string]$ExpectedHash
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Update payload is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ([Int64]$item.Length -ne $ExpectedSize) {
        throw "Update payload size mismatch."
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $actualHash = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    if ($actualHash -ne $ExpectedHash.ToLowerInvariant()) {
        throw "Update payload SHA256 mismatch."
    }
}

function Rollback-Update {
    param(
        [string]$Target,
        [string]$Backup,
        [object]$StartedProcess
    )
    Write-UpdateLog "Rollback started."
    if ($null -ne $StartedProcess) {
        try {
            if (-not $StartedProcess.HasExited) {
                Stop-Process -Id $StartedProcess.Id -Force -ErrorAction SilentlyContinue
                $StartedProcess.WaitForExit(10000)
            }
        }
        catch {
            Write-UpdateLog "Unable to stop failed new process: $($_.Exception.Message)"
        }
    }
    if (-not (Test-Path -LiteralPath $Backup -PathType Leaf)) {
        throw "Rollback backup is missing."
    }
    if (Test-Path -LiteralPath $Target -PathType Leaf) {
        $failedPath = "$Target.failed"
        if (Test-Path -LiteralPath $failedPath) {
            Remove-Item -LiteralPath $failedPath -Force
        }
        Wait-TargetProcesses -Target $Target -TimeoutSeconds 15
        Invoke-FileReplaceWithRetry -Source $Backup -Target $Target -Backup $failedPath
        if (Test-Path -LiteralPath $failedPath) {
            Remove-Item -LiteralPath $failedPath -Force
        }
    }
    else {
        Move-Item -LiteralPath $Backup -Destination $Target
    }
    Write-UpdateLog "Rollback completed."
}

$target = [System.IO.Path]::GetFullPath([string]$plan.target_path)
$staged = [System.IO.Path]::GetFullPath([string]$plan.staged_path)
$replacement = [System.IO.Path]::GetFullPath([string]$plan.replacement_path)
$backup = [System.IO.Path]::GetFullPath([string]$plan.backup_path)
$health = [System.IO.Path]::GetFullPath([string]$plan.health_path)
$expectedProcessPath = [System.IO.Path]::GetFullPath([string]$plan.wait_process_path)
$token = [string]$plan.health_token
$version = [string]$plan.version
$healthTimeoutSeconds = [int]$plan.health_timeout_seconds
$committed = $false
$started = $null
$installMutex = [System.Threading.Mutex]::new(
    $false,
    "Local\PhotoCollectSelfUpdate"
)
$ownsInstallMutex = $false
Remove-Item -LiteralPath $failurePath -Force -ErrorAction SilentlyContinue

try {
    $ownsInstallMutex = $installMutex.WaitOne(0)
}
catch [Threading.AbandonedMutexException] {
    $ownsInstallMutex = $true
}
catch {
    Write-UpdateLog "Unable to acquire updater lock: $($_.Exception.Message)"
    Write-UpdateFailure -Message $_.Exception.Message -Recovered $true
    $installMutex.Dispose()
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    exit 1
}
if (-not $ownsInstallMutex) {
    Write-UpdateLog "Another PhotoCollect update is already running."
    $installMutex.Dispose()
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    exit 2
}

try {
    if (
        -not [string]::Equals(
            $target,
            $expectedProcessPath,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Update plan target and process path do not match."
    }
    if ($healthTimeoutSeconds -lt 5 -or $healthTimeoutSeconds -gt 120) {
        throw "Update health timeout is outside the allowed range."
    }

    Write-UpdateLog "Waiting for PhotoCollect PID $($plan.wait_pid)."
    $oldProcess = Get-Process -Id ([int]$plan.wait_pid) -ErrorAction SilentlyContinue
    if ($null -ne $oldProcess) {
        try {
            $actualProcessPath = [System.IO.Path]::GetFullPath([string]$oldProcess.Path)
        }
        catch {
            if ($oldProcess.HasExited) {
                $oldProcess = $null
            }
            else {
                throw "Unable to verify the process that must exit."
            }
        }
        if (
            $null -ne $oldProcess -and
            -not [string]::Equals(
                $actualProcessPath,
                $expectedProcessPath,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Write-UpdateLog "PID was reused by another executable; no wait is required."
            $oldProcess = $null
        }
        if ($null -ne $oldProcess) {
            $oldProcess.WaitForExit(120000)
            if (-not $oldProcess.HasExited) {
                throw "PhotoCollect did not exit within 120 seconds."
            }
        }
    }
    Wait-TargetProcesses -Target $target -TimeoutSeconds 30

    Assert-UpdateFile -Path $staged -ExpectedSize ([Int64]$plan.expected_size) -ExpectedHash ([string]$plan.expected_sha256)
    Copy-Item -LiteralPath $staged -Destination $replacement -Force
    Assert-UpdateFile -Path $replacement -ExpectedSize ([Int64]$plan.expected_size) -ExpectedHash ([string]$plan.expected_sha256)

    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "Installed PhotoCollect executable is missing."
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Force
    }
    Invoke-FileReplaceWithRetry -Source $replacement -Target $target -Backup $backup
    $committed = $true
    Write-UpdateLog "Atomic replacement committed."

    if (Test-Path -LiteralPath $health) {
        Remove-Item -LiteralPath $health -Force
    }
    $quotedHealth = '"' + $health + '"'
    $arguments = @(
        "--post-update-health",
        $quotedHealth,
        "--post-update-token",
        $token
    )
    $started = Start-Process -FilePath $target -ArgumentList $arguments -PassThru
    Write-UpdateLog "Started version $version as PID $($started.Id)."

    $deadline = [DateTime]::UtcNow.AddSeconds($healthTimeoutSeconds)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $health -PathType Leaf) {
            try {
                $healthData = Get-Content -LiteralPath $health -Raw -Encoding UTF8 | ConvertFrom-Json
                if (
                    [string]$healthData.token -eq $token -and
                    [string]$healthData.version -eq $version
                ) {
                    $healthy = $true
                    break
                }
            }
            catch {
                Write-UpdateLog "Health marker is not ready yet."
            }
        }
        if ($started.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $healthy) {
        throw "New PhotoCollect version did not report healthy startup."
    }

    Write-UpdateLog "Update succeeded."
    Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $health -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $failureMessage = $_.Exception.Message
    Write-UpdateLog "Update failed: $failureMessage"
    $recovered = $false
    if ($committed) {
        try {
            Rollback-Update -Target $target -Backup $backup -StartedProcess $started
            $recovered = $true
        }
        catch {
            $rollbackMessage = $_.Exception.Message
            Write-UpdateLog "Rollback failed: $rollbackMessage"
            $failureMessage = "$failureMessage; rollback failed: $rollbackMessage"
        }
    }
    elseif (Test-Path -LiteralPath $target -PathType Leaf) {
        $recovered = $true
    }
    Write-UpdateFailure -Message $failureMessage -Recovered $recovered
    if ($recovered) {
        try {
            Start-Process -FilePath $target | Out-Null
            if ($committed) {
                Write-UpdateLog "Rollback completed and previous version restarted."
            }
            else {
                Write-UpdateLog "Previous version restarted after pre-commit failure."
            }
        }
        catch {
            Write-UpdateLog "Unable to restart previous version: $($_.Exception.Message)"
        }
    }
    exit 1
}
finally {
    Remove-Item -LiteralPath $replacement -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    if ($ownsInstallMutex) {
        try {
            $installMutex.ReleaseMutex()
        }
        catch {
            Write-UpdateLog "Unable to release updater lock: $($_.Exception.Message)"
        }
    }
    $installMutex.Dispose()
}
"""


class UpdateError(RuntimeError):
    """Base exception for update failures that can be shown to the user."""


class ManifestError(UpdateError):
    """Raised when the release manifest is missing or cannot be trusted."""


class UpdateNetworkError(UpdateError):
    """Raised when the update server cannot be reached safely."""


class DownloadError(UpdateError):
    """Raised when an update payload is incomplete or fails verification."""


class DownloadCancelled(DownloadError):
    """Raised when the user cancels an update download."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateNetworkError("更新服务器返回了不允许的跳转")


def _default_opener():
    return build_opener(_RejectRedirects())


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status)


def _open_exact(opener, request: Request, *, timeout: float):
    try:
        response = opener.open(request, timeout=timeout)
    except UpdateError:
        raise
    except HTTPError as exc:
        raise UpdateNetworkError(f"更新服务器返回 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateNetworkError(f"无法连接更新服务器：{reason}") from exc

    if _response_status(response) != 200:
        response.close()
        raise UpdateNetworkError(f"更新服务器返回 HTTP {_response_status(response)}")
    final_url = response.geturl()
    if final_url != request.full_url:
        response.close()
        raise UpdateNetworkError("更新服务器返回了不允许的跳转")
    return response


def _version_key(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise ManifestError(f"版本号格式无效：{value!r}")
    parts = [int(part) for part in value.split(".")]
    if any(part > 999_999 for part in parts):
        raise ManifestError("版本号分段超出允许范围")
    return tuple(parts + [0] * (4 - len(parts)))


def is_newer_version(candidate: str, current: str) -> bool:
    """Return whether *candidate* is numerically newer than *current*."""

    return _version_key(candidate) > _version_key(current)


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    version: str
    release_id: str
    published_at: str
    notes: str
    file_name: str
    file_url: str
    size_bytes: int
    sha256: str
    manifest_url: str

    def is_newer_than(self, current_version: str) -> bool:
        return is_newer_version(self.version, current_version)


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    current_version: str
    manifest: UpdateManifest
    available: bool


@dataclass(frozen=True, slots=True)
class InstallPlan:
    plan_path: Path
    helper_path: Path
    target_path: Path
    staged_path: Path
    backup_path: Path
    health_path: Path
    failure_path: Path
    health_token: str


def _required_string(
    source: Mapping[str, Any],
    key: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise ManifestError(f"更新清单字段 {key} 必须是文本")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > max_length:
        raise ManifestError(f"更新清单字段 {key} 无效")
    return value


def _normalized_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ManifestError("更新地址格式无效") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ManifestError("更新地址必须使用 HTTP 或 HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ManifestError("更新地址主机无效")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def parse_update_manifest(
    payload: Mapping[str, Any],
    *,
    manifest_url: str = DEFAULT_MANIFEST_URL,
) -> UpdateManifest:
    """Validate a v1 PhotoCollect manifest and return its immutable model."""

    if not isinstance(payload, Mapping):
        raise ManifestError("更新清单根节点必须是对象")
    if payload.get("schema_version") != 1:
        raise ManifestError("不支持的更新清单版本")
    if payload.get("app") != "PhotoCollect":
        raise ManifestError("更新清单不属于 PhotoCollect")
    if payload.get("channel") != "stable":
        raise ManifestError("只接受 stable 更新通道")

    version = _required_string(payload, "version", max_length=32)
    _version_key(version)
    sha_source = payload.get("file")
    if not isinstance(sha_source, Mapping):
        raise ManifestError("更新清单缺少 file 对象")
    sha256 = _required_string(sha_source, "sha256", max_length=64).lower()
    if not _SHA256_PATTERN.fullmatch(sha256):
        raise ManifestError("更新文件 SHA-256 无效")

    expected_release_id = f"PhotoCollect:{version}:{sha256}"
    release_id = _required_string(payload, "release_id", max_length=160)
    if release_id != expected_release_id:
        raise ManifestError("更新清单 release_id 与版本或哈希不一致")

    published_at = _required_string(payload, "published_at", max_length=64)
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("更新清单发布时间无效") from exc
    if published.tzinfo is None:
        raise ManifestError("更新清单发布时间必须带时区")

    notes = _required_string(
        payload,
        "notes",
        max_length=4_000,
        allow_empty=True,
    )
    file_name = _required_string(sha_source, "name", max_length=128)
    expected_name = f"PhotoCollect-v{version}.exe"
    if file_name != expected_name:
        raise ManifestError("更新文件名与版本号不一致")

    file_url = _required_string(sha_source, "url", max_length=2_048)
    manifest_parts = urlsplit(manifest_url)
    file_parts = urlsplit(file_url)
    if _normalized_origin(file_url) != _normalized_origin(manifest_url):
        raise ManifestError("更新文件必须与清单来自同一服务器")
    expected_path = manifest_parts.path.rsplit("/", 1)[0] + "/" + expected_name
    if (
        file_parts.path != expected_path
        or file_parts.query
        or file_parts.fragment
        or manifest_parts.query
        or manifest_parts.fragment
    ):
        raise ManifestError("更新文件 URL 路径无效")

    size_bytes = sha_source.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or size_bytes > MAX_UPDATE_BYTES
    ):
        raise ManifestError("更新文件大小无效")

    return UpdateManifest(
        version=version,
        release_id=release_id,
        published_at=published_at,
        notes=notes,
        file_name=file_name,
        file_url=file_url,
        size_bytes=size_bytes,
        sha256=sha256,
        manifest_url=manifest_url,
    )


def check_for_update(
    current_version: str,
    *,
    manifest_url: str = DEFAULT_MANIFEST_URL,
    opener=None,
    timeout: float = 10.0,
) -> UpdateCheckResult:
    """Fetch and validate the release pointer.

    This function performs network I/O and is intentionally UI-agnostic so the
    Qt layer can always call it from a background worker.
    """

    _version_key(current_version)
    request = Request(
        manifest_url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": f"PhotoCollect/{current_version}",
        },
        method="GET",
    )
    client = opener or _default_opener()
    response = _open_exact(client, request, timeout=timeout)
    try:
        body = response.read(MAX_MANIFEST_BYTES + 1)
    except (TimeoutError, OSError) as exc:
        raise UpdateNetworkError("读取更新清单失败") from exc
    finally:
        response.close()
    if len(body) > MAX_MANIFEST_BYTES:
        raise ManifestError("更新清单体积异常")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("更新清单不是有效的 UTF-8 JSON") from exc
    manifest = parse_update_manifest(payload, manifest_url=manifest_url)
    return UpdateCheckResult(
        current_version=current_version,
        manifest=manifest,
        available=manifest.is_newer_than(current_version),
    )


def default_update_dir() -> Path:
    local_appdata = Path(
        os.getenv("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    )
    return local_appdata / "PhotoCollect" / "updates"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cancel_requested(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def download_update(
    manifest: UpdateManifest,
    *,
    destination_dir: Path | None = None,
    opener=None,
    timeout: float = 30.0,
    chunk_size: int = 1024 * 1024,
    progress_callback=None,
    cancel_event=None,
) -> Path:
    """Download an update into a private staging directory and verify it."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    target_dir = Path(destination_dir or default_update_dir())
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = target_dir / manifest.file_name

    if _cancel_requested(cancel_event):
        raise DownloadCancelled("更新下载已取消")

    if final_path.is_file():
        try:
            if (
                final_path.stat().st_size == manifest.size_bytes
                and sha256_file(final_path) == manifest.sha256
            ):
                if progress_callback is not None:
                    progress_callback(manifest.size_bytes, manifest.size_bytes)
                return final_path
        except OSError:
            pass
        try:
            final_path.unlink()
        except OSError as exc:
            raise DownloadError("无法清理损坏的旧更新文件") from exc

    partial_path = target_dir / (
        f".{manifest.file_name}.{secrets.token_hex(8)}.part"
    )
    request = Request(
        manifest.file_url,
        headers={
            "Accept": "application/octet-stream",
            "Cache-Control": "no-cache",
            "User-Agent": f"PhotoCollect/{manifest.version}",
        },
        method="GET",
    )
    response = None
    try:
        response = _open_exact(
            opener or _default_opener(),
            request,
            timeout=timeout,
        )
        content_length = response.headers.get("Content-Length")
        if content_length not in {None, ""}:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError) as exc:
                raise DownloadError("更新服务器返回了无效的文件大小") from exc
            if declared_size != manifest.size_bytes:
                raise DownloadError("更新文件大小与清单不一致")

        digest = hashlib.sha256()
        downloaded = 0
        with partial_path.open("xb") as stream:
            while True:
                if _cancel_requested(cancel_event):
                    raise DownloadCancelled("更新下载已取消")
                try:
                    chunk = response.read(chunk_size)
                except (TimeoutError, OSError) as exc:
                    raise DownloadError("读取更新文件时连接中断") from exc
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > manifest.size_bytes:
                    raise DownloadError("更新文件大于清单声明的大小")
                stream.write(chunk)
                digest.update(chunk)
                if progress_callback is not None:
                    progress_callback(downloaded, manifest.size_bytes)
            stream.flush()
            os.fsync(stream.fileno())

        if downloaded != manifest.size_bytes:
            raise DownloadError("更新文件下载不完整")
        if digest.hexdigest() != manifest.sha256:
            raise DownloadError("更新文件 SHA-256 校验失败")
        os.replace(partial_path, final_path)
        if progress_callback is not None and downloaded == 0:
            progress_callback(0, manifest.size_bytes)
        return final_path
    except UpdateError:
        raise
    except OSError as exc:
        raise DownloadError(f"无法保存更新文件：{exc}") from exc
    finally:
        if response is not None:
            response.close()
        try:
            partial_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def prepare_install_plan(
    manifest: UpdateManifest,
    staged_path: Path,
    *,
    target_executable: Path | None = None,
    current_pid: int | None = None,
    update_dir: Path | None = None,
    health_timeout_seconds: int = 45,
) -> InstallPlan:
    """Create the immutable hand-off consumed after the GUI exits."""

    install_target = Path(target_executable or os.sys.executable)
    try:
        install_target = install_target.resolve(strict=True)
    except OSError as exc:
        raise UpdateError("找不到当前 PhotoCollect 程序文件") from exc
    if install_target.suffix.lower() != ".exe" or not install_target.is_file():
        raise UpdateError("自动安装只支持正在运行的 Windows EXE")

    work_dir = Path(update_dir or default_update_dir())
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()
    try:
        staged = Path(staged_path).resolve(strict=True)
    except OSError as exc:
        raise DownloadError("已下载的更新文件不存在") from exc
    if staged.parent != work_dir or staged.name != manifest.file_name:
        raise DownloadError("更新文件不在受控的暂存目录中")
    try:
        staged_size = staged.stat().st_size
        staged_hash = sha256_file(staged)
    except OSError as exc:
        raise DownloadError("无法复核已下载的更新文件") from exc
    if staged_size != manifest.size_bytes or staged_hash != manifest.sha256:
        raise DownloadError("已下载的更新文件校验失败")
    if staged == install_target:
        raise UpdateError("更新暂存文件不能覆盖当前程序")

    pid = int(current_pid if current_pid is not None else os.getpid())
    if pid <= 0:
        raise UpdateError("当前进程编号无效")
    if (
        isinstance(health_timeout_seconds, bool)
        or not isinstance(health_timeout_seconds, int)
        or not 5 <= health_timeout_seconds <= 120
    ):
        raise UpdateError("更新启动验收超时必须在 5 到 120 秒之间")
    token = secrets.token_hex(16)
    replacement = install_target.with_name(
        f".{install_target.name}.{token}.new"
    )
    backup = install_target.with_name(f"{install_target.name}.previous")
    health = work_dir / f"health-{token}.json"
    failure = work_dir / "last-install-failure.json"
    log_path = work_dir / f"install-v{manifest.version}.log"
    helper_path = work_dir / f"apply-update-{token}.ps1"
    plan_path = work_dir / f"install-v{manifest.version}-{token}.json"

    plan_data = {
        "schema_version": 1,
        "app": "PhotoCollect",
        "version": manifest.version,
        "release_id": manifest.release_id,
        "target_path": str(install_target),
        "staged_path": str(staged),
        "replacement_path": str(replacement),
        "backup_path": str(backup),
        "health_path": str(health),
        "failure_path": str(failure),
        "health_token": token,
        "log_path": str(log_path),
        "expected_size": manifest.size_bytes,
        "expected_sha256": manifest.sha256,
        "wait_pid": pid,
        "wait_process_path": str(install_target),
        "health_timeout_seconds": health_timeout_seconds,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(helper_path, WINDOWS_UPDATE_HELPER)
    _atomic_write_text(
        plan_path,
        json.dumps(plan_data, ensure_ascii=False, indent=2),
    )
    return InstallPlan(
        plan_path=plan_path,
        helper_path=helper_path,
        target_path=install_target,
        staged_path=staged,
        backup_path=backup,
        health_path=health,
        failure_path=failure,
        health_token=token,
    )


def discard_install_plan(plan: InstallPlan) -> None:
    """Remove a not-yet-launched hand-off without touching the verified payload."""

    for path in (plan.plan_path, plan.helper_path, plan.health_path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "Unable to remove abandoned updater file: %s",
                path,
            )


def consume_post_update_health_args(
    argv: list[str],
) -> tuple[Path, str] | None:
    """Remove updater-only arguments before Qt parses the command line."""

    if not argv:
        return None
    cleaned = [argv[0]]
    marker = None
    token = None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in {"--post-update-health", "--post-update-token"}:
            if index + 1 >= len(argv):
                raise UpdateError(f"{argument} 缺少参数")
            if argument == "--post-update-health":
                if marker is not None:
                    raise UpdateError("重复的更新健康标记参数")
                marker = Path(argv[index + 1])
            else:
                if token is not None:
                    raise UpdateError("重复的更新健康令牌参数")
                token = argv[index + 1]
            index += 2
            continue
        cleaned.append(argument)
        index += 1
    argv[:] = cleaned
    if marker is None and token is None:
        return None
    if marker is None or token is None:
        raise UpdateError("更新健康参数不完整")
    if not _HEALTH_TOKEN_PATTERN.fullmatch(token):
        raise UpdateError("更新健康令牌无效")
    return marker, token


def write_post_update_health(
    marker_path: Path,
    token: str,
    version: str,
    *,
    allowed_dir: Path | None = None,
) -> Path:
    """Acknowledge that the new GUI initialized successfully."""

    if not _HEALTH_TOKEN_PATTERN.fullmatch(token):
        raise UpdateError("更新健康令牌无效")
    _version_key(version)
    root = Path(allowed_dir or default_update_dir())
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    marker = Path(marker_path).resolve()
    if marker.parent != root or marker.name != f"health-{token}.json":
        raise UpdateError("更新健康标记路径超出受控目录")
    payload = {
        "schema_version": 1,
        "app": "PhotoCollect",
        "version": version,
        "token": token,
        "pid": os.getpid(),
        "ready_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_text(
        marker,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return marker


def consume_update_failure(
    *,
    update_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return and remove the most recent updater failure report."""

    root = Path(update_dir or default_update_dir())
    root.mkdir(parents=True, exist_ok=True)
    marker = root.resolve() / "last-install-failure.json"
    if not marker.is_file():
        return None
    try:
        if marker.stat().st_size > 64 * 1024:
            raise ValueError("更新失败记录体积异常")
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("app") != "PhotoCollect"
            or payload.get("status") != "failed"
        ):
            raise ValueError("更新失败记录格式无效")
        version = payload.get("version")
        message = payload.get("message")
        log_path = payload.get("log_path")
        recovered = payload.get("recovered")
        if recovered is not None and not isinstance(recovered, bool):
            raise ValueError("更新失败记录的恢复状态无效")
        if not all(isinstance(item, str) and item.strip() for item in (
            version,
            message,
            log_path,
        )):
            raise ValueError("更新失败记录字段不完整")
        _version_key(version)
        return {
            "version": version.strip(),
            "message": message.strip(),
            "log_path": log_path.strip(),
            "recovered": recovered,
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        logging.getLogger(__name__).exception("Unable to read updater failure result")
        return None
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "Unable to remove consumed updater failure result: %s",
                marker,
            )


def cleanup_successful_update_backup(
    target_executable: Path | None = None,
) -> bool:
    """Remove the desktop rollback copy after startup health is confirmed."""
    target = Path(target_executable or sys.executable)
    if target.suffix.lower() != ".exe":
        return False
    backup = target.with_name(target.name + ".previous")
    try:
        backup.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Unable to remove successful-update rollback copy: %s",
            backup,
        )
        return False
    return not backup.exists()


def launch_install_helper(
    plan: InstallPlan,
    *,
    popen_factory=None,
    powershell_executable: str | Path | None = None,
):
    """Start the out-of-process Windows updater and return its process handle."""

    if os.name != "nt":
        raise UpdateError("自动安装目前只支持 Windows")
    helper = Path(plan.helper_path).resolve()
    plan_path = Path(plan.plan_path).resolve()
    if (
        not helper.is_file()
        or not plan_path.is_file()
        or helper.parent != plan_path.parent
    ):
        raise UpdateError("更新助手文件不完整")
    if powershell_executable is None:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = (
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            program_files / "PowerShell" / "7" / "pwsh.exe",
        )
        powershell_executable = next((path for path in candidates if path.is_file()), None)
        if powershell_executable is None:
            powershell_executable = shutil.which("pwsh.exe")
        if powershell_executable is None:
            raise UpdateError("找不到 PowerShell 更新助手（需 Windows PowerShell 或 PowerShell 7）")

    command = [
        str(powershell_executable),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-PlanPath",
        str(plan_path),
    ]
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )
    factory = popen_factory or subprocess.Popen
    try:
        return factory(
            command,
            cwd=str(helper.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateError(f"无法启动更新助手：{exc}") from exc


def parse_ntfy_release_event(
    line: bytes | str,
    *,
    expected_manifest_url: str = DEFAULT_MANIFEST_URL,
) -> dict[str, Any] | None:
    """Return a normalized release notification, ignoring all other events."""

    if isinstance(line, bytes):
        if len(line) > MAX_NTFY_EVENT_BYTES:
            return None
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(line, str):
        if len(line.encode("utf-8", errors="ignore")) > MAX_NTFY_EVENT_BYTES:
            return None
        text = line
    else:
        return None
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(envelope, Mapping)
        or envelope.get("event") != "message"
        or envelope.get("topic") != NTFY_TOPIC
    ):
        return None
    message = envelope.get("message")
    if not isinstance(message, str) or len(message) > 8_192:
        return None
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or payload.get("type") != "release.ready":
        return None
    try:
        version = _required_string(payload, "version", max_length=32)
        _version_key(version)
        sha256 = _required_string(payload, "sha256", max_length=64).lower()
        if not _SHA256_PATTERN.fullmatch(sha256):
            return None
        release_id = _required_string(payload, "release_id", max_length=160)
        if release_id != f"PhotoCollect:{version}:{sha256}":
            return None
        manifest_url = _required_string(
            payload,
            "manifest_url",
            max_length=2_048,
        )
        if manifest_url != expected_manifest_url:
            return None
        size_bytes = payload.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
            or size_bytes > MAX_UPDATE_BYTES
        ):
            return None
    except ManifestError:
        return None
    return {
        "type": "release.ready",
        "release_id": release_id,
        "version": version,
        "manifest_url": manifest_url,
        "sha256": sha256,
        "size_bytes": size_bytes,
    }


def watch_update_notifications(
    stop_event,
    on_release,
    *,
    ntfy_url: str = DEFAULT_NTFY_URL,
    expected_manifest_url: str = DEFAULT_MANIFEST_URL,
    opener=None,
    timeout: float = 80.0,
    retry_delay: float = 60.0,
) -> None:
    """Listen for ntfy release messages until *stop_event* is set.

    Authentication is deliberately not embedded in the EXE. The NAS topic may
    grant anonymous read-only access; if it does not, periodic manifest checks
    remain the fallback.
    """

    logger = logging.getLogger(__name__)
    stream_url = ntfy_url + ("&" if "?" in ntfy_url else "?") + "since=latest"
    client = opener or _default_opener()
    while not stop_event.is_set():
        response = None
        try:
            request = Request(
                stream_url,
                headers={
                    "Accept": "application/x-ndjson",
                    "Cache-Control": "no-cache",
                    "User-Agent": "PhotoCollect/update-listener",
                },
                method="GET",
            )
            response = _open_exact(client, request, timeout=timeout)
            while not stop_event.is_set():
                line = response.readline(MAX_NTFY_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_NTFY_EVENT_BYTES:
                    continue
                release = parse_ntfy_release_event(
                    line,
                    expected_manifest_url=expected_manifest_url,
                )
                if release is not None:
                    on_release(release)
        except (UpdateError, TimeoutError, OSError) as exc:
            if not stop_event.is_set():
                logger.info("ntfy update listener unavailable: %s", exc)
        finally:
            if response is not None:
                response.close()
        if not stop_event.is_set():
            stop_event.wait(retry_delay)
