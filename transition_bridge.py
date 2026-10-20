"""Build the one-time native bridge used to cross the v5.16.1 updater bug."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
_PAYLOAD_RESOURCE_NAME = "PhotoCollect.RealPayload"
_WORKER_RESOURCE_NAME = "PhotoCollect.TransitionWorker"
_MODULE_ROOT = Path(__file__).resolve().parent
_BOOTSTRAP_TEMPLATE = _MODULE_ROOT / "transition_bridge_bootstrap.cs"
_WORKER_TEMPLATE = _MODULE_ROOT / "transition_bridge_worker.cs"


@dataclass(frozen=True, slots=True)
class TransitionBridgeBuild:
    output_path: Path
    payload_path: Path
    payload_size: int
    payload_sha256: str
    version: str
    health_timeout_seconds: int


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _default_csc_path() -> Path:
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidates = (
        windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 .NET Framework C# 编译器 csc.exe")


def _render_template(template_path: Path, replacements: dict[str, str]) -> str:
    source = template_path.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        source = source.replace(placeholder, value)
    unresolved = sorted(set(re.findall(r"__[A-Z0-9_]+__", source)))
    if unresolved:
        raise RuntimeError(
            f"桥接 C# 模板仍有未替换字段：{', '.join(unresolved)}"
        )
    return source


def _compile_csharp(
    compiler: Path,
    source: Path,
    output: Path,
    *,
    resources: tuple[tuple[Path, str], ...] = (),
    references: tuple[str, ...] = (),
) -> None:
    command = [
        str(compiler),
        "/nologo",
        "/target:winexe",
        "/platform:x64",
        "/optimize+",
        f"/out:{output}",
        *(f"/r:{reference}" for reference in references),
        *(f"/resource:{path},{name},private" for path, name in resources),
        str(source),
    ]
    completed = subprocess.run(
        command,
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError("桥接 EXE 编译失败：\n" + completed.stdout.strip())


def build_transition_bridge(
    payload_path: Path,
    output_path: Path,
    *,
    version: str,
    csc_path: Path | None = None,
    health_timeout_seconds: int = 180,
) -> TransitionBridgeBuild:
    """Compile a native bridge EXE containing one verified PhotoCollect EXE."""

    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"不支持的版本号：{version!r}")
    if (
        isinstance(health_timeout_seconds, bool)
        or not isinstance(health_timeout_seconds, int)
        or not 5 <= health_timeout_seconds <= 300
    ):
        raise ValueError("真实程序健康检查超时必须在 5 到 300 秒之间")
    payload = Path(payload_path).resolve(strict=True)
    if payload.suffix.lower() != ".exe" or not payload.is_file():
        raise ValueError("桥接载荷必须是现存的 EXE 文件")
    payload_size = payload.stat().st_size
    if payload_size <= 0:
        raise ValueError("桥接载荷不能为空")
    payload_sha256 = sha256_file(payload)

    output = Path(output_path).resolve()
    if output.suffix.lower() != ".exe":
        raise ValueError("桥接输出必须使用 .exe 扩展名")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output == payload:
        raise ValueError("桥接输出不能覆盖真实 PhotoCollect EXE")
    compiler = Path(csc_path or _default_csc_path()).resolve(strict=True)
    for template in (_BOOTSTRAP_TEMPLATE, _WORKER_TEMPLATE):
        if not template.is_file():
            raise FileNotFoundError(f"找不到桥接 C# 模板：{template}")

    with tempfile.TemporaryDirectory(prefix="PhotoCollect-transition-bridge-") as raw:
        work = Path(raw)
        worker_source = work / "PhotoCollectTransitionWorker.cs"
        worker = work / "PhotoCollectTransitionWorker.exe"
        common = {
            "__FILE_VERSION__": f"{version}.0",
            "__VERSION__": version,
            "__PAYLOAD_SIZE__": str(payload_size),
            "__PAYLOAD_SHA256__": payload_sha256,
            "__HEALTH_TIMEOUT_SECONDS__": str(health_timeout_seconds),
        }
        worker_source.write_text(
            _render_template(_WORKER_TEMPLATE, common),
            encoding="utf-8",
            newline="\n",
        )
        _compile_csharp(compiler, worker_source, worker)
        worker_size = worker.stat().st_size
        worker_sha256 = sha256_file(worker)

        source = work / "PhotoCollectTransitionBridge.cs"
        compiled = work / "PhotoCollect.exe"
        source.write_text(
            _render_template(
                _BOOTSTRAP_TEMPLATE,
                {
                    **common,
                    "__WORKER_SIZE__": str(worker_size),
                    "__WORKER_SHA256__": worker_sha256,
                },
            ),
            encoding="utf-8",
            newline="\n",
        )
        _compile_csharp(
            compiler,
            source,
            compiled,
            resources=(
                (payload, _PAYLOAD_RESOURCE_NAME),
                (worker, _WORKER_RESOURCE_NAME),
            ),
            references=("System.Windows.Forms.dll",),
        )
        temporary = output.with_name(f".{output.name}.new")
        try:
            if temporary.exists():
                temporary.unlink()
            shutil.copyfile(compiled, temporary)
            if sha256_file(temporary) != sha256_file(compiled):
                raise OSError("桥接 EXE 临时副本哈希不一致")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    return TransitionBridgeBuild(
        output_path=output,
        payload_path=payload,
        payload_size=payload_size,
        payload_sha256=payload_sha256,
        version=version,
        health_timeout_seconds=health_timeout_seconds,
    )


__all__ = ["TransitionBridgeBuild", "build_transition_bridge", "sha256_file"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the one-time native PhotoCollect transition bridge."
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--health-timeout", type=int, default=180)
    args = parser.parse_args()
    result = build_transition_bridge(
        args.payload,
        args.output,
        version=args.version,
        health_timeout_seconds=args.health_timeout,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "version": result.version,
                "output": str(result.output_path),
                "payload": str(result.payload_path),
                "payload_size": result.payload_size,
                "payload_sha256": result.payload_sha256,
                "health_timeout_seconds": result.health_timeout_seconds,
                "bridge_size": result.output_path.stat().st_size,
                "bridge_sha256": sha256_file(result.output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
