"""Read-only workspace health checks for PhotoCollect."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping


META_NAME = "meta.json"
META_BAK_NAME = "meta.json.bak"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
MAX_ISSUES = 500
# The photo classifier knows slots 1-5; any other prediction (its OTHER class)
# means "looks like slots 6-8".
MODEL_SLOT_CATEGORIES = (1, 2, 3, 4, 5)
SUSPECT_CATEGORY_THRESHOLD = 0.95
OTHER_SLOTS_TEXT = "6-8 类（加固/底梁/铅封）"


class HealthScanCancelled(Exception):
    """Raised when a caller cancels a read-only workspace scan."""


@dataclass(frozen=True)
class HealthIssue:
    code: str
    severity: str
    title: str
    detail: str
    task_name: str = ""


@dataclass(frozen=True)
class HealthReport:
    root: Path
    checked_at: str
    duration_ms: int
    task_count: int
    referenced_photo_count: int
    internal_directory_count: int
    used_backup: bool
    issues: tuple[HealthIssue, ...]
    truncated: bool = False
    classified_photo_count: int = 0

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def to_text(self) -> str:
        lines = [
            "集装箱照片收集工具 数据检查",
            f"工作目录：{self.root}",
            f"检查时间：{self.checked_at}",
            f"任务：{self.task_count}，已引用照片：{self.referenced_photo_count}",
            f"错误：{self.error_count}，警告：{self.warning_count}",
        ]
        if self.classified_photo_count:
            lines.append(f"模型核对照片类别：{self.classified_photo_count} 张")
        if not self.issues:
            lines.append("未发现问题。")
        else:
            severity_labels = {"error": "错误", "warning": "警告", "info": "提示"}
            for issue in self.issues:
                prefix = severity_labels.get(issue.severity, issue.severity)
                task = f" [{issue.task_name}]" if issue.task_name else ""
                lines.append(f"{prefix}{task} {issue.title}：{issue.detail}")
        if self.truncated:
            lines.append(f"问题数量超过 {MAX_ISSUES} 条，结果已截断。")
        return "\n".join(lines)


def _read_json_dict(path: Path) -> tuple[dict | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "文件内容不是 JSON 对象"
    return value, ""


def _category_text(category: int, labels: Mapping[int, str] | None) -> str:
    if category not in MODEL_SLOT_CATEGORIES:
        return OTHER_SLOTS_TEXT
    label = (labels or {}).get(category)
    return f"{category} 类（{label}）" if label else f"{category} 类"


def suspected_wrong_category(
    placed: int,
    prediction,
    threshold: float = SUSPECT_CATEGORY_THRESHOLD,
) -> int | None:
    """The slot a confident prediction points to when it disagrees with `placed`.

    Returns the predicted slot 1-5, 0 for "looks like 6-8", or None when the
    photo is where it belongs or the model is not sure enough.
    """
    if prediction.confidence < threshold:
        return None
    predicted = prediction.category if prediction.category in MODEL_SLOT_CATEGORIES else 0
    if placed in MODEL_SLOT_CATEGORIES:
        return None if predicted == placed else predicted
    return predicted or None


def scan_data_health(
    root: Path | str,
    *,
    task_name_validator: Callable[[str], bool] | None = None,
    category_count: int = 8,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    photo_classifier=None,
    category_threshold: float = SUSPECT_CATEGORY_THRESHOLD,
    category_labels: Mapping[int, str] | None = None,
) -> HealthReport:
    """Inspect one workspace without creating, changing, or deleting files.

    With a `photo_classifier` (anything with `predict(path)` returning a
    category and confidence), every referenced photo is also classified and a
    confident disagreement with its slot becomes a warning.
    """
    started = time.perf_counter()
    root_path = Path(root)
    issues: list[HealthIssue] = []
    truncated = False
    classified_photo_count = 0

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise HealthScanCancelled()

    def report_progress(current: int, total: int, label: str) -> None:
        if progress_callback is not None:
            progress_callback(current, total, label)

    check_cancelled()
    report_progress(0, 1, "读取工作目录")

    def add_issue(
        code: str,
        severity: str,
        title: str,
        detail: str,
        task_name: str = "",
    ) -> None:
        nonlocal truncated
        if len(issues) >= MAX_ISSUES:
            truncated = True
            return
        issues.append(HealthIssue(code, severity, title, detail, task_name))

    if not root_path.is_dir():
        add_issue(
            "root_unavailable",
            "error",
            "工作目录不可用",
            str(root_path),
        )
        return _build_report(root_path, started, 0, 0, 0, False, issues, truncated)

    if not os.access(root_path, os.R_OK):
        add_issue("root_not_readable", "error", "目录不可读", str(root_path))
    if not os.access(root_path, os.W_OK):
        add_issue("root_not_writable", "warning", "目录不可写", str(root_path))

    try:
        directories = tuple(path for path in root_path.iterdir() if path.is_dir())
    except OSError as exc:
        add_issue("root_scan_failed", "error", "无法读取目录", str(exc))
        return _build_report(root_path, started, 0, 0, 0, False, issues, truncated)
    internal_directories = tuple(path for path in directories if path.name.startswith("."))
    task_directories = tuple(path for path in directories if not path.name.startswith("."))
    task_names = {path.name for path in task_directories}
    progress_total = max(2, len(task_directories) + 2)
    report_progress(1, progress_total, "检查元数据")
    check_cancelled()

    primary = root_path / META_NAME
    backup = root_path / META_BAK_NAME
    metadata: dict = {}
    used_backup = False
    primary_error = ""
    if primary.exists():
        loaded, primary_error = _read_json_dict(primary)
        if loaded is not None:
            metadata = loaded
        else:
            add_issue(
                "metadata_invalid",
                "error",
                "主元数据无法读取",
                primary_error,
            )
    if (not metadata and primary_error) or (not primary.exists() and backup.exists()):
        loaded_backup, backup_error = _read_json_dict(backup)
        if loaded_backup is not None:
            metadata = loaded_backup
            used_backup = True
            add_issue(
                "metadata_backup_used",
                "warning",
                "已使用元数据备份检查",
                str(backup),
            )
        elif backup.exists():
            add_issue(
                "metadata_backup_invalid",
                "error",
                "元数据备份也无法读取",
                backup_error,
            )

    for key in metadata:
        check_cancelled()
        if not isinstance(key, str) or key.startswith("__"):
            continue
        if key.startswith("."):
            add_issue(
                "internal_metadata_entry",
                "warning",
                "内部目录被写入业务元数据",
                key,
            )
        elif key not in task_names:
            add_issue(
                "missing_task_directory",
                "warning",
                "元数据对应的任务目录不存在",
                key,
                key,
            )

    referenced_photo_count = 0
    for task_index, box_dir in enumerate(task_directories, start=2):
        check_cancelled()
        box_name = box_dir.name
        report_progress(task_index, progress_total, f"检查任务 {box_name}")
        if task_name_validator is not None:
            try:
                valid_name = bool(task_name_validator(box_name))
            except Exception:
                valid_name = False
            if not valid_name:
                add_issue(
                    "invalid_task_name",
                    "warning",
                    "任务箱号格式异常",
                    box_name,
                    box_name,
                )

        raw_box_meta = metadata.get(box_name, {})
        if not isinstance(raw_box_meta, Mapping):
            add_issue(
                "invalid_task_metadata",
                "error",
                "任务元数据格式错误",
                "任务内容不是 JSON 对象",
                box_name,
            )
            raw_box_meta = {}

        referenced_names: set[str] = set()
        seen_references: dict[str, int] = {}
        placed_photos: list[tuple[int, Path]] = []
        for category in range(1, category_count + 1):
            check_cancelled()
            key = str(category)
            if key not in raw_box_meta:
                continue
            values = raw_box_meta.get(key)
            if not isinstance(values, list):
                add_issue(
                    "invalid_category_value",
                    "error",
                    "分类照片清单格式错误",
                    f"分类 {category} 应为列表",
                    box_name,
                )
                continue
            for filename in values:
                check_cancelled()
                if not isinstance(filename, str) or not filename:
                    add_issue(
                        "invalid_photo_reference",
                        "warning",
                        "照片引用格式错误",
                        f"分类 {category} 中存在非文件名内容",
                        box_name,
                    )
                    continue
                filename_key = filename.casefold()
                if filename_key in seen_references:
                    add_issue(
                        "duplicate_photo_reference",
                        "warning",
                        "照片被多个分类重复引用",
                        (
                            f"{filename}：分类 {seen_references[filename_key]} "
                            f"和分类 {category}"
                        ),
                        box_name,
                    )
                    continue
                seen_references[filename_key] = category
                try:
                    source = (box_dir / filename).resolve()
                    source.relative_to(box_dir.resolve())
                except (OSError, ValueError):
                    add_issue(
                        "unsafe_photo_reference",
                        "error",
                        "照片引用越出任务目录",
                        filename,
                        box_name,
                    )
                    continue
                if not source.is_file():
                    add_issue(
                        "missing_photo_reference",
                        "warning",
                        "元数据引用的照片不存在",
                        filename,
                        box_name,
                    )
                    continue
                referenced_names.add(source.name.casefold())
                referenced_photo_count += 1
                placed_photos.append((category, source))

        if photo_classifier is not None:
            for category, source in placed_photos:
                check_cancelled()
                try:
                    prediction = photo_classifier.predict(source)
                except Exception:
                    # Deleted since it was listed, or not a decodable image.
                    continue
                classified_photo_count += 1
                looks_like = suspected_wrong_category(category, prediction, category_threshold)
                if looks_like is None:
                    continue
                add_issue(
                    "suspected_wrong_category",
                    "warning",
                    "疑似放错类别",
                    (
                        f"{source.name} 放在 {_category_text(category, category_labels)}，"
                        f"看起来像 {_category_text(looks_like, category_labels)}"
                        f"（模型把握 {prediction.confidence:.0%}）"
                    ),
                    box_name,
                )

        try:
            disk_images = tuple(
                path for path in box_dir.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
        except OSError as exc:
            add_issue(
                "task_scan_failed",
                "error",
                "无法读取任务目录",
                str(exc),
                box_name,
            )
            continue
        for image in disk_images:
            check_cancelled()
            if image.name.casefold() not in referenced_names:
                add_issue(
                    "orphan_photo",
                    "warning",
                    "照片文件未被元数据引用",
                    image.name,
                    box_name,
                )

    check_cancelled()
    report_progress(progress_total, progress_total, "整理检查结果")
    return _build_report(
        root_path,
        started,
        len(task_directories),
        referenced_photo_count,
        len(internal_directories),
        used_backup,
        issues,
        truncated,
        classified_photo_count,
    )


def _build_report(
    root: Path,
    started: float,
    task_count: int,
    referenced_photo_count: int,
    internal_directory_count: int,
    used_backup: bool,
    issues: list[HealthIssue],
    truncated: bool,
    classified_photo_count: int = 0,
) -> HealthReport:
    return HealthReport(
        root=root,
        checked_at=datetime.now().isoformat(timespec="seconds"),
        duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
        task_count=task_count,
        referenced_photo_count=referenced_photo_count,
        internal_directory_count=internal_directory_count,
        used_backup=used_backup,
        issues=tuple(issues),
        truncated=truncated,
        classified_photo_count=classified_photo_count,
    )
