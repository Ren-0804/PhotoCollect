"""Local storage operations for PhotoCollect.

The UI calls this module through small interfaces for trash, duplicate detection,
and handoff export. File moves and metadata updates stay together here so callers
do not need to coordinate partial disk state.
"""

from __future__ import annotations

import csv
import copy
import io
import json
import hashlib
import logging
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from metadata_services import metadata_write_lock
from task_services import resolve_task_photos


log = logging.getLogger(__name__)


PRIVATE_DIR_NAME = ".photocollect"
TRASH_INDEX_NAME = "index.json"
META_NAME = "meta.json"
META_BAK_NAME = "meta.json.bak"


@dataclass(frozen=True)
class TaskExportRecord:
    name: str
    size_type: str
    ocr_status: str
    task_status: str
    photo_count: int
    missing: tuple[str, ...]
    last_updated: str
    required_categories: tuple[int, ...] = ()
    missing_category_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ExportSummary:
    output_path: Path
    task_count: int
    photo_count: int
    source_photo_bytes: int = 0
    exported_photo_bytes: int = 0
    optimized_photo_count: int = 0


class ExportCancelled(Exception):
    pass


def bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_image_hash_index(box_dir: Path | str, box_meta: dict) -> dict[str, str]:
    """Return one existing filename for each image hash in a task."""
    box_path = Path(box_dir).resolve()
    index: dict[str, str] = {}
    seen_names: set[str] = set()
    for category in map(str, range(1, 9)):
        files = box_meta.get(category, []) if isinstance(box_meta, dict) else []
        if not isinstance(files, list):
            continue
        for filename in files:
            if not isinstance(filename, str) or filename in seen_names:
                continue
            seen_names.add(filename)
            path = (box_path / filename).resolve()
            try:
                path.relative_to(box_path)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                index.setdefault(file_sha256(path), filename)
            except OSError:
                continue
    try:
        disk_images = [
            path for path in box_path.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
        ]
    except OSError:
        disk_images = []
    for path in disk_images:
        if path.name in seen_names:
            continue
        try:
            index.setdefault(file_sha256(path), path.name)
        except OSError:
            continue
    return index


def export_handoff_package(
    root: Path | str,
    destination: Path | str,
    records: Sequence[TaskExportRecord],
    category_names: Mapping[int, str],
    cancel_event: Any = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    image_transformer: Callable[[Path], tuple[str, bytes] | None] | None = None,
) -> ExportSummary:
    """Write a flat ``container/photo`` handoff ZIP and publish it atomically."""
    root_path = Path(root).resolve()
    output_path = Path(destination)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.partial"
    )

    meta = _read_dict(root_path / META_NAME, root_path / META_BAK_NAME)
    exported: dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in meta.items()
        if isinstance(key, str) and key.startswith("__")
    }
    photos: list[tuple[Path, str, str]] = []
    clean_records: list[TaskExportRecord] = []
    global_requirements = meta.get("__requirements__")

    for record in records:
        box_dir = (root_path / record.name).resolve()
        try:
            box_dir.relative_to(root_path)
        except ValueError:
            continue
        box_meta = meta.get(record.name, {})
        if not box_dir.is_dir() or not isinstance(box_meta, dict):
            continue
        fallback_requirements = (
            global_requirements
            if isinstance(global_requirements, list)
            else list(record.required_categories)
        )
        photo_state = resolve_task_photos(
            box_dir,
            box_meta,
            fallback_requirements,
            category_count=8,
        )
        clean_entry: dict[str, Any] = {
            str(category): []
            for category in range(1, 9)
        }
        for photo in photo_state.photos:
            photos.append((photo.path, record.name, str(photo.category)))
        for special_key, value in box_meta.items():
            if isinstance(special_key, str) and special_key.startswith("__"):
                clean_entry[special_key] = value
        exported[record.name] = clean_entry

        if photo_state.required_categories:
            missing = tuple(
                _category_status_label(
                    category,
                    category_names.get(category, f"分类{category}"),
                )
                for category in photo_state.missing_category_ids
            )
        else:
            missing = record.missing
        actual_photo_count = photo_state.photo_count
        task_status = record.task_status
        if missing and task_status == "已完成":
            task_status = "待补"
        clean_records.append(
            replace(
                record,
                photo_count=actual_photo_count,
                missing=missing,
                task_status=task_status,
                required_categories=photo_state.required_categories,
                missing_category_ids=photo_state.missing_category_ids,
            )
        )

    exported["__categories__"] = {
        str(category): _category_display_name(
            category,
            category_names.get(category, f"分类{category}"),
        )
        for category in range(1, 9)
    }
    exported["__export__"] = {
        "format_version": 2,
        "layout": "箱号/照片",
        "task_count": len(clean_records),
        "photo_count": len(photos),
    }

    total_steps = len(photos) + 2
    done = 0
    source_photo_bytes = 0
    exported_photo_bytes = 0
    optimized_photo_count = 0
    used_names: dict[str, set[str]] = {}
    written_photos: set[str] = set()
    try:
        with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            _raise_if_cancelled(cancel_event)
            csv_buffer = io.StringIO(newline="")
            writer = csv.writer(csv_buffer)
            writer.writerow(["箱号", "箱型", "OCR核对状态", "任务状态", "照片数", "缺失项", "最后更新时间"])
            for record in clean_records:
                writer.writerow([
                    record.name,
                    record.size_type,
                    record.ocr_status,
                    record.task_status,
                    record.photo_count,
                    ";".join(record.missing),
                    record.last_updated,
                ])
            archive.writestr("summary.csv", csv_buffer.getvalue().encode("utf-8-sig"))
            done += 1
            _notify_progress(progress_callback, done, total_steps, "summary.csv")

            for record in clean_records:
                archive.writestr(f"{record.name}/", b"")

            for source, box_name, category_key in photos:
                _raise_if_cancelled(cancel_event)
                source_size = source.stat().st_size
                export_name = source.name
                payload = None
                transformed = None
                if image_transformer is not None:
                    try:
                        transformed = image_transformer(source)
                    except Exception:
                        log.exception("Image export optimization failed for %s", source)
                if transformed is not None:
                    candidate_name, candidate_payload = transformed
                    candidate_name = Path(str(candidate_name)).name
                    if candidate_name and isinstance(candidate_payload, (bytes, bytearray)) and candidate_payload:
                        export_name = candidate_name
                        payload = bytes(candidate_payload)
                        optimized_photo_count += 1

                export_name = _unique_filename(export_name, used_names.setdefault(box_name, set()))
                exported[box_name][category_key].append(export_name)
                arcname = f"{box_name}/{export_name}"
                if payload is None:
                    archive.write(source, arcname)
                    exported_photo_bytes += source_size
                else:
                    archive.writestr(arcname, payload)
                    exported_photo_bytes += len(payload)
                written_photos.add(arcname)
                source_photo_bytes += source_size
                done += 1
                _notify_progress(progress_callback, done, total_steps, source.name)

            _raise_if_cancelled(cancel_event)
            for record in clean_records:
                exported[record.name]["__summary__"] = {
                    "container_no": record.name,
                    "size_type": record.size_type,
                    "ocr_status": record.ocr_status,
                    "task_status": record.task_status,
                    "photo_count": record.photo_count,
                    "missing": list(record.missing),
                    "last_updated": record.last_updated,
                }
            manifest_photos = {
                f"{record.name}/{filename}"
                for record in clean_records
                for category in map(str, range(1, 9))
                for filename in exported[record.name][category]
            }
            if manifest_photos != written_photos:
                raise RuntimeError("导出照片与 data.json 清单不一致")
            archive.writestr("data.json", json.dumps(exported, ensure_ascii=False, indent=2).encode("utf-8"))
            done += 1
            _notify_progress(progress_callback, done, total_steps, "data.json")
        _raise_if_cancelled(cancel_event)
        os.replace(partial_path, output_path)
    except Exception:
        try:
            partial_path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "Failed to remove incomplete export %s",
                partial_path,
                exc_info=True,
            )
        raise

    return ExportSummary(
        output_path=output_path,
        task_count=len(clean_records),
        photo_count=len(photos),
        source_photo_bytes=source_photo_bytes,
        exported_photo_bytes=exported_photo_bytes,
        optimized_photo_count=optimized_photo_count,
    )


def _category_display_name(index: int, label: str) -> str:
    clean = re.sub(r"^\s*\d+\s*", "", str(label)).strip()
    clean = re.sub(r'[\\/:*?"<>|]', "_", clean) or f"分类{index}"
    return clean


def _category_status_label(index: int, label: str) -> str:
    return re.sub(r"^\s*\d+\s*", "", str(label)).strip() or f"分类{index}"


def _unique_filename(filename: str, used: set[str]) -> str:
    clean = re.sub(r'[\\/:*?"<>|]', "_", Path(filename).name) or "photo"
    stem = Path(clean).stem or "photo"
    suffix = Path(clean).suffix
    candidate = clean
    sequence = 2
    while candidate.lower() in used:
        candidate = f"{stem}_{sequence}{suffix}"
        sequence += 1
    used.add(candidate.lower())
    return candidate


def _raise_if_cancelled(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled("Export cancelled")


def _notify_progress(
    callback: Callable[[int, int, str], None] | None,
    current: int,
    total: int,
    label: str,
) -> None:
    if callback is not None:
        callback(current, total, label)


def _read_dict(path: Path, fallback_path: Path | None = None) -> dict:
    candidates = [path]
    if fallback_path is not None and fallback_path != path:
        candidates.append(fallback_path)
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _atomic_write_json(path: Path, data: Any, backup_path: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, backup_path)
            except OSError:
                pass
    finally:
        if tmp.exists():
            tmp.unlink()


def _serialized_trash_mutation(operation):
    @wraps(operation)
    def serialized(self, *args, **kwargs):
        with metadata_write_lock(self.meta_path):
            return operation(self, *args, **kwargs)

    return serialized


class TrashStore:
    """Move tasks and photos to a recoverable trash area inside a work root."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.trash_dir = self.root / PRIVATE_DIR_NAME / "trash"
        self.items_dir = self.trash_dir / "items"
        self.index_path = self.trash_dir / TRASH_INDEX_NAME
        self.meta_path = self.root / META_NAME

    def list_entries(self) -> list[dict]:
        raw = _read_dict(self.index_path)
        entries = raw.get("entries", [])
        if not isinstance(entries, list):
            return []
        clean = [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]
        return sorted(clean, key=lambda entry: entry.get("deleted_at", ""), reverse=True)

    @_serialized_trash_mutation
    def trash_task(self, box_name: str) -> dict:
        source = self._task_path(box_name)
        if not source.is_dir():
            raise FileNotFoundError(f"Task does not exist: {box_name}")

        meta = self._read_meta()
        snapshot = meta.get(box_name, {})
        entry_id = uuid.uuid4().hex
        item_dir = self.items_dir / entry_id
        stored = item_dir / "task"
        entry = {
            "id": entry_id,
            "kind": "task",
            "box_name": box_name,
            "category": None,
            "filename": None,
            "deleted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "storage_relpath": stored.relative_to(self.root).as_posix(),
            "metadata_snapshot": snapshot if isinstance(snapshot, dict) else {},
        }

        previous_entries = self.list_entries()
        item_dir.mkdir(parents=True, exist_ok=False)
        try:
            shutil.move(str(source), str(stored))
            self._write_entries([entry, *previous_entries])
            meta.pop(box_name, None)
            self._write_meta(meta)
        except Exception:
            if stored.exists() and not source.exists():
                shutil.move(str(stored), str(source))
            self._write_entries(previous_entries)
            shutil.rmtree(item_dir, ignore_errors=True)
            raise
        return entry

    @_serialized_trash_mutation
    def trash_photo(self, box_name: str, category: str | int, filename: str) -> dict:
        category_key = str(category)
        box_dir = self._task_path(box_name)
        source = (box_dir / filename).resolve()
        source.relative_to(box_dir)
        if not source.is_file():
            raise FileNotFoundError(f"Photo does not exist: {filename}")

        meta = self._read_meta()
        box_meta = meta.get(box_name, {})
        if not isinstance(box_meta, dict):
            box_meta = {}
        files = box_meta.get(category_key, [])
        if not isinstance(files, list):
            files = []

        entry_id = uuid.uuid4().hex
        item_dir = self.items_dir / entry_id
        stored = item_dir / "photo" / source.name
        entry = {
            "id": entry_id,
            "kind": "photo",
            "box_name": box_name,
            "category": category_key,
            "filename": source.name,
            "deleted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "storage_relpath": stored.relative_to(self.root).as_posix(),
            "metadata_snapshot": copy.deepcopy(box_meta),
        }

        previous_entries = self.list_entries()
        stored.parent.mkdir(parents=True, exist_ok=False)
        try:
            shutil.move(str(source), str(stored))
            self._write_entries([entry, *previous_entries])
            box_meta[category_key] = [item for item in files if item != source.name]
            meta[box_name] = box_meta
            self._write_meta(meta)
        except Exception:
            if stored.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stored), str(source))
            self._write_entries(previous_entries)
            shutil.rmtree(item_dir, ignore_errors=True)
            raise
        return entry

    @_serialized_trash_mutation
    def restore(self, entry_id: str) -> dict:
        entries = self.list_entries()
        entry = next((item for item in entries if item.get("id") == entry_id), None)
        if entry is None:
            raise KeyError(f"Trash entry does not exist: {entry_id}")
        if entry.get("kind") == "photo":
            return self._restore_photo(entry, entries)
        if entry.get("kind") != "task":
            raise ValueError(f"Unsupported trash entry kind: {entry.get('kind')}")

        target = self._task_path(str(entry.get("box_name", "")))
        if target.exists():
            raise FileExistsError(f"Task already exists: {target.name}")
        source = self._stored_path(entry)
        if not source.is_dir():
            raise FileNotFoundError(f"Trashed task data is missing: {source}")

        meta = self._read_meta()
        if target.name in meta:
            raise FileExistsError(f"Task metadata already exists: {target.name}")
        previous_meta = copy.deepcopy(meta)
        remaining = [item for item in entries if item.get("id") != entry_id]
        try:
            shutil.move(str(source), str(target))
            snapshot = entry.get("metadata_snapshot", {})
            meta[target.name] = snapshot if isinstance(snapshot, dict) else {}
            self._write_meta(meta)
            self._write_entries(remaining)
            shutil.rmtree(source.parent, ignore_errors=True)
        except Exception:
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
            try:
                self._write_meta(previous_meta)
                self._write_entries(entries)
            except OSError:
                pass
            raise
        return entry

    @_serialized_trash_mutation
    def purge(self, entry_id: str) -> dict:
        entries = self.list_entries()
        entry = next((item for item in entries if item.get("id") == entry_id), None)
        if entry is None:
            raise KeyError(f"Trash entry does not exist: {entry_id}")
        item_dir = (self.items_dir / entry_id).resolve()
        item_dir.relative_to(self.items_dir.resolve())
        remaining = [item for item in entries if item.get("id") != entry_id]
        self._write_entries(remaining)
        if item_dir.exists():
            shutil.rmtree(item_dir)
        return entry

    @_serialized_trash_mutation
    def clear(self) -> int:
        entries = self.list_entries()
        self._write_entries([])
        for entry in entries:
            item_dir = (self.items_dir / str(entry.get("id", ""))).resolve()
            item_dir.relative_to(self.items_dir.resolve())
            if item_dir.exists():
                shutil.rmtree(item_dir)
        return len(entries)

    def _restore_photo(self, entry: dict, entries: list[dict]) -> dict:
        box_name = str(entry.get("box_name", ""))
        category = str(entry.get("category", ""))
        filename = str(entry.get("filename", ""))
        box_dir = self._task_path(box_name)
        if not box_dir.is_dir():
            raise FileNotFoundError(f"Task no longer exists: {box_name}")
        target = (box_dir / filename).resolve()
        target.relative_to(box_dir)
        if target.exists():
            raise FileExistsError(f"Photo already exists: {filename}")
        source = self._stored_path(entry)
        if not source.is_file():
            raise FileNotFoundError(f"Trashed photo data is missing: {source}")

        meta = self._read_meta()
        previous_meta = copy.deepcopy(meta)
        box_meta = meta.get(box_name, {})
        if not isinstance(box_meta, dict):
            box_meta = {}
        for index in range(1, 9):
            referenced = box_meta.get(str(index), [])
            if isinstance(referenced, list) and filename in referenced:
                raise FileExistsError(f"Photo metadata already exists: {filename}")
        files = box_meta.get(category, [])
        if not isinstance(files, list):
            files = []
        remaining = [item for item in entries if item.get("id") != entry.get("id")]
        try:
            shutil.move(str(source), str(target))
            if filename not in files:
                files.append(filename)
            box_meta[category] = files
            meta[box_name] = box_meta
            self._write_meta(meta)
            self._write_entries(remaining)
            shutil.rmtree(source.parent.parent, ignore_errors=True)
        except Exception:
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
            try:
                self._write_meta(previous_meta)
                self._write_entries(entries)
            except OSError:
                pass
            raise
        return entry

    def _task_path(self, box_name: str) -> Path:
        target = (self.root / box_name).resolve()
        target.relative_to(self.root)
        if target == self.root or target.name.startswith("."):
            raise ValueError(f"Invalid task name: {box_name}")
        return target

    def _stored_path(self, entry: dict) -> Path:
        entry_id = str(entry.get("id", ""))
        item_dir = (self.items_dir / entry_id).resolve()
        item_dir.relative_to(self.items_dir.resolve())
        source = (self.root / str(entry.get("storage_relpath", ""))).resolve()
        source.relative_to(item_dir)
        return source

    def _read_meta(self) -> dict:
        return _read_dict(self.meta_path, self.root / META_BAK_NAME)

    def _write_entries(self, entries: list[dict]) -> None:
        _atomic_write_json(self.index_path, {"entries": entries})

    def _write_meta(self, meta: dict) -> None:
        _atomic_write_json(self.meta_path, meta, self.root / META_BAK_NAME)
