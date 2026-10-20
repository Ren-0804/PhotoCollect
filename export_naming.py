from __future__ import annotations

import re
import string
from dataclasses import dataclass
from datetime import datetime


DEFAULT_EXPORT_TEMPLATES = {
    "zip": "PhotoCollect_{YYYYMMDD_HHMM}.zip",
    "excel": "PhotoCollect_采集看板_{YYYYMMDD_HHMM}.xlsx",
}

_EXTENSIONS = {"zip": ".zip", "excel": ".xlsx"}
_ALLOWED_FIELDS = {"YYYYMMDD", "YYYYMMDD_HHMM", "箱数", "照片数", "范围"}
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class RenderedExportName:
    filename: str
    used_fallback: bool = False
    error: str = ""


def _legacy_default(kind: str, now: datetime) -> str:
    if kind == "zip":
        return f"PhotoCollect_{now:%Y%m%d_%H%M}.zip"
    return f"PhotoCollect_采集看板_{now:%Y%m%d_%H%M}.xlsx"


def _render_template(template: str, values: dict[str, str]) -> str:
    pieces: list[str] = []
    for literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        pieces.append(literal)
        if field_name is None:
            continue
        if field_name not in _ALLOWED_FIELDS:
            raise ValueError(f"不支持的占位符：{field_name}")
        if format_spec or conversion:
            raise ValueError(f"占位符不支持格式参数：{field_name}")
        pieces.append(values[field_name])
    return "".join(pieces)


def _normalize_extension(filename: str, extension: str) -> str:
    filename = re.sub(r"\.(?:zip|xlsx)$", "", filename, flags=re.IGNORECASE)
    return filename + extension


def _validate_basename(filename: str) -> None:
    stem = filename.rsplit(".", 1)[0].rstrip(" .")
    if not stem or stem in {".", ".."}:
        raise ValueError("文件名不能为空")
    if stem.upper() in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"Windows 保留文件名不可用：{stem}")
    if len(filename) > 180:
        raise ValueError("文件名过长")


def render_export_filename(
    kind: str,
    template: str,
    *,
    task_count: int,
    photo_count: int,
    scope: str,
    now: datetime | None = None,
) -> RenderedExportName:
    """Render a safe destination basename without changing export contents."""
    if kind not in _EXTENSIONS:
        raise ValueError(f"Unsupported export kind: {kind}")
    current_time = now or datetime.now()
    fallback = _legacy_default(kind, current_time)
    raw_template = str(template or "").strip()
    if not raw_template:
        return RenderedExportName(fallback, True, "文件名模板不能为空")
    if "/" in raw_template or "\\" in raw_template:
        return RenderedExportName(fallback, True, "文件名模板不能包含路径")

    values = {
        "YYYYMMDD": current_time.strftime("%Y%m%d"),
        "YYYYMMDD_HHMM": current_time.strftime("%Y%m%d_%H%M"),
        "箱数": str(max(0, int(task_count or 0))),
        "照片数": str(max(0, int(photo_count or 0))),
        "范围": str(scope or "全部任务"),
    }
    try:
        rendered = _render_template(raw_template, values)
        # Asterisks in examples are often visual separators, but Windows cannot
        # store them. Removing invalid characters keeps the intended wording.
        rendered = _INVALID_WINDOWS_CHARS.sub("", rendered).strip().rstrip(" .")
        rendered = _normalize_extension(rendered, _EXTENSIONS[kind])
        _validate_basename(rendered)
    except (KeyError, TypeError, ValueError) as exc:
        return RenderedExportName(fallback, True, str(exc))
    return RenderedExportName(rendered)
