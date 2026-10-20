"""Background-safe image hashing, persistence, deduplication, and metadata commit."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from metadata_services import read_metadata, update_metadata
from storage_services import build_image_hash_index, bytes_sha256, file_sha256


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}


@dataclass(frozen=True)
class ImageImportSource:
    label: str
    path: Path | None = None
    payload: bytes | None = None
    extension: str = ".jpg"
    source_digest: str = ""

    @classmethod
    def from_path(cls, path: Path | str) -> "ImageImportSource":
        source_path = Path(path)
        return cls(
            label=source_path.name,
            path=source_path,
            extension=source_path.suffix.lower(),
        )

    @classmethod
    def from_payload(
        cls,
        label: str,
        payload: bytes,
        *,
        extension: str = ".jpg",
        source_digest: str = "",
    ) -> "ImageImportSource":
        return cls(
            label=str(label),
            payload=bytes(payload),
            extension=str(extension).lower(),
            source_digest=str(source_digest),
        )


@dataclass(frozen=True)
class ImageImportSummary:
    added_names: tuple[str, ...]
    duplicate_names: tuple[str, ...]
    duplicate_count: int
    skipped_count: int
    failed_count: int
    cancelled: bool
    failures: tuple[str, ...]


def _next_destination(
    box_dir: Path,
    box_name: str,
    category: int,
    extension: str,
) -> tuple[str, Path]:
    base = f"{box_name}-{category}"
    counter = 1
    while True:
        filename = (
            f"{base}_{counter}{extension}"
            if counter > 1
            else f"{base}{extension}"
        )
        destination = box_dir / filename
        if not destination.exists():
            return filename, destination
        counter += 1


def _publish_local_copy(source: Path, destination: Path) -> None:
    partial = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.partial"
    )
    try:
        shutil.copy2(source, partial)
        os.replace(partial, destination)
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _publish_payload(payload: bytes, destination: Path) -> None:
    partial = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.partial"
    )
    try:
        with partial.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, destination)
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def import_task_images(
    root_dir: Path | str,
    box_name: str,
    category: int,
    sources: Iterable[ImageImportSource],
    *,
    cancel_event=None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> ImageImportSummary:
    """Persist an ordered import batch and atomically merge it into meta.json."""
    root = Path(root_dir)
    box_dir = root / str(box_name)
    box_dir.mkdir(parents=True, exist_ok=True)
    meta_path = root / "meta.json"
    backup_path = root / "meta.json.bak"
    source_list = list(sources)
    raw_meta = read_metadata(meta_path, backup_path)
    box_meta = raw_meta.get(str(box_name), {})
    if not isinstance(box_meta, dict):
        box_meta = {}
    hash_index = build_image_hash_index(box_dir, box_meta)

    added_names: list[str] = []
    added_paths: list[Path] = []
    duplicate_names: list[str] = []
    duplicate_count = skipped_count = failed_count = 0
    failures: list[str] = []
    cancelled = False

    for index, source in enumerate(source_list, start=1):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if progress_callback is not None:
            progress_callback(index, len(source_list), source.label)
        extension = str(source.extension or "").lower()
        if extension not in IMAGE_EXTENSIONS:
            skipped_count += 1
            continue

        try:
            if source.path is not None:
                digest = file_sha256(source.path)
            elif source.payload is not None:
                digest = bytes_sha256(source.payload)
            else:
                skipped_count += 1
                continue
            existing_name = hash_index.get(digest)
            if existing_name is None and source.source_digest:
                existing_name = hash_index.get(source.source_digest)
            if existing_name is not None:
                duplicate_count += 1
                duplicate_names.append(existing_name)
                continue

            filename, destination = _next_destination(
                box_dir,
                str(box_name),
                int(category),
                extension,
            )
            if source.path is not None:
                _publish_local_copy(source.path, destination)
            else:
                _publish_payload(source.payload or b"", destination)
            added_names.append(filename)
            added_paths.append(destination)
            hash_index[digest] = filename
            if source.source_digest:
                hash_index[source.source_digest] = filename
        except (OSError, shutil.Error, ValueError) as exc:
            failed_count += 1
            failures.append(f"{source.label}: {exc}")

    if added_names:
        def merge_imported_files(all_data):
            task_data = all_data.get(str(box_name), {})
            if not isinstance(task_data, dict):
                task_data = {}
            category_key = str(int(category))
            current = task_data.get(category_key, [])
            if not isinstance(current, list):
                current = []
            for filename in added_names:
                if filename not in current:
                    current.append(filename)
            task_data[category_key] = current
            if int(category) in (4, 5):
                task_data['__ocr__'] = {
                    'pending_source': added_names[-1],
                    'verification_status': 'unreadable',
                }
            all_data[str(box_name)] = task_data

        try:
            update_metadata(meta_path, merge_imported_files, backup_path)
        except Exception:
            for destination in added_paths:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    return ImageImportSummary(
        added_names=tuple(added_names),
        duplicate_names=tuple(duplicate_names),
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        cancelled=cancelled,
        failures=tuple(failures),
    )
