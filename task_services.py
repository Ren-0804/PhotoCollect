"""Pure task-photo state resolution shared by UI, reports, and exports."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


@dataclass(frozen=True)
class TaskPhoto:
    category: int
    filename: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class TaskPhotoState:
    photos: tuple[TaskPhoto, ...]
    required_categories: tuple[int, ...]
    missing_category_ids: tuple[int, ...]
    category_counts: tuple[int, ...]
    has_auto_ocr_photo: bool

    @property
    def photo_count(self) -> int:
        return len(self.photos)

    @property
    def source_photo_bytes(self) -> int:
        return sum(photo.size_bytes for photo in self.photos)

    def count_for(self, category: int) -> int:
        if 1 <= category <= len(self.category_counts):
            return self.category_counts[category - 1]
        return 0

    def counts_dict(self) -> dict[str, int]:
        return {
            str(category): count
            for category, count in enumerate(self.category_counts, start=1)
        }


@dataclass(frozen=True)
class TaskSnapshot:
    name: str
    size_type: str
    ocr_container: str
    ocr_status: str
    task_state: str
    photo_count: int
    source_photo_bytes: int
    required_categories: tuple[int, ...]
    missing_category_ids: tuple[int, ...]
    missing_labels: tuple[str, ...]
    category_counts: tuple[int, ...]
    updated_ts: float
    last_updated: str
    box_number_valid: bool
    has_auto_ocr_photo: bool
    ocr_enabled: bool

    def to_summary(self) -> dict:
        state_text = {
            "complete": "已完成",
            "anomaly": "异常",
        }.get(self.task_state, f"缺: {','.join(self.missing_labels)}")
        filter_text = (
            f"{self.name} {self.photo_count} {state_text} {' '.join(self.missing_labels)} "
            f"{self.ocr_container} {self.size_type} {self.ocr_status} {self.task_state}"
        ).lower()
        return {
            "name": self.name,
            "txt": f"{self.name} {self.photo_count}张 {state_text}",
            "ok": self.task_state == "complete",
            "pic_cnt": self.photo_count,
            "source_photo_bytes": self.source_photo_bytes,
            "missing": list(self.missing_labels),
            "missing_count": len(self.missing_labels),
            "missing_category_ids": list(self.missing_category_ids),
            "required_categories": list(self.required_categories),
            "category_counts": {
                str(category): count
                for category, count in enumerate(self.category_counts, start=1)
            },
            "ocr_container": self.ocr_container,
            "ocr_size_type": self.size_type,
            "ocr_status": self.ocr_status,
            "ocr_enabled": self.ocr_enabled,
            "task_state": self.task_state,
            "box_number_valid": self.box_number_valid,
            "has_auto_ocr_photo": self.has_auto_ocr_photo,
            "updated_ts": self.updated_ts,
            "last_updated": self.last_updated,
            "filter": filter_text,
        }


@dataclass(frozen=True)
class ExportPreflight:
    kind: str
    scope: str
    task_count: int
    complete_count: int
    pending_count: int
    anomaly_count: int
    photo_count: int
    source_photo_bytes: int = 0


def build_export_preflight(
    task_summaries: Iterable[Mapping],
    *,
    kind: str,
    scope: str,
    source_photo_bytes: int | None = None,
) -> ExportPreflight:
    summaries = tuple(
        summary for summary in task_summaries
        if isinstance(summary, Mapping)
    )
    states = [
        str(summary.get("task_state", "pending"))
        for summary in summaries
    ]
    photo_count = 0
    for summary in summaries:
        try:
            photo_count += max(0, int(summary.get("pic_cnt", 0) or 0))
        except (TypeError, ValueError):
            continue
    if source_photo_bytes is None:
        source_photo_bytes = 0
        for summary in summaries:
            try:
                source_photo_bytes += max(
                    0,
                    int(summary.get("source_photo_bytes", 0) or 0),
                )
            except (TypeError, ValueError):
                continue
    return ExportPreflight(
        kind=str(kind),
        scope=str(scope),
        task_count=len(summaries),
        complete_count=states.count("complete"),
        pending_count=states.count("pending"),
        anomaly_count=states.count("anomaly"),
        photo_count=photo_count,
        source_photo_bytes=max(0, int(source_photo_bytes or 0)),
    )


def normalize_requirements(
    value,
    fallback,
    category_count: int,
) -> tuple[int, ...]:
    source = value if isinstance(value, list) else fallback
    if not isinstance(source, (list, tuple, set)):
        source = ()
    return tuple(sorted({
        item
        for item in source
        if isinstance(item, int) and not isinstance(item, bool)
        and 1 <= item <= category_count
    }))


def _infer_app_named_category(
    box_name: str,
    filename: str,
    category_count: int,
) -> int | None:
    """Infer a category only from filenames created by PhotoCollect itself."""
    candidate = Path(filename)
    if candidate.name != filename or candidate.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    prefix = f"{box_name}-"
    stem = candidate.stem
    if not stem.casefold().startswith(prefix.casefold()):
        return None
    remainder = stem[len(prefix):]
    category_text, separator, counter_text = remainder.partition("_")
    if not category_text.isdigit():
        return None
    if separator and (not counter_text.isdigit() or int(counter_text) < 2):
        return None
    category = int(category_text)
    return category if 1 <= category <= category_count else None


def resolve_task_photos(
    box_dir: Path | str,
    box_meta: Mapping | None,
    global_requirements: Iterable[int] | None,
    *,
    category_count: int,
    auto_ocr_categories: Iterable[int] = (),
) -> TaskPhotoState:
    """Resolve the actual, safe, unique files represented by one task."""
    box_path = Path(box_dir).resolve()
    data = box_meta if isinstance(box_meta, Mapping) else {}
    required = normalize_requirements(
        data.get("__requirements__"),
        global_requirements,
        category_count,
    )
    auto_ocr = {
        item for item in auto_ocr_categories
        if isinstance(item, int) and 1 <= item <= category_count
    }
    counts = [0] * category_count
    photos: list[TaskPhoto] = []
    seen_names: set[str] = set()

    for category in range(1, category_count + 1):
        values = data.get(str(category), [])
        if not isinstance(values, list):
            continue
        for filename in values:
            if not isinstance(filename, str) or not filename:
                continue
            filename_key = filename.casefold()
            if filename_key in seen_names:
                continue
            try:
                source = (box_path / filename).resolve()
                source.relative_to(box_path)
            except (OSError, ValueError):
                continue
            try:
                source_stat = source.stat()
            except FileNotFoundError:
                continue
            except OSError:
                # Keep a registered photo visible during a transient share
                # failure so its thumbnail loader can retry the same file.
                seen_names.add(filename_key)
                counts[category - 1] += 1
                photos.append(TaskPhoto(category, filename, source, 0))
                continue
            if not stat.S_ISREG(source_stat.st_mode):
                continue
            seen_names.add(filename_key)
            counts[category - 1] += 1
            photos.append(TaskPhoto(category, filename, source, max(0, source_stat.st_size)))

    # A file can survive a network interruption or an older metadata-write bug.
    # Recover only names generated by this app; arbitrary images remain unassigned.
    try:
        disk_files = sorted(box_path.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        disk_files = []
    for disk_file in disk_files:
        filename = disk_file.name
        filename_key = filename.casefold()
        if filename_key in seen_names:
            continue
        category = _infer_app_named_category(box_path.name, filename, category_count)
        if category is None:
            continue
        try:
            source = disk_file.resolve()
            source.relative_to(box_path)
            source_stat = source.stat()
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            continue
        seen_names.add(filename_key)
        counts[category - 1] += 1
        photos.append(TaskPhoto(category, filename, source, max(0, source_stat.st_size)))

    missing = tuple(
        category
        for category in required
        if counts[category - 1] == 0
    )
    return TaskPhotoState(
        photos=tuple(photos),
        required_categories=required,
        missing_category_ids=missing,
        category_counts=tuple(counts),
        has_auto_ocr_photo=any(
            counts[category - 1] > 0
            for category in auto_ocr
        ),
    )
