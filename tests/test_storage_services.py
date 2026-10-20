import json
import multiprocessing
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from storage_services import (
    TaskExportRecord,
    TrashStore,
    ExportCancelled,
    build_image_hash_index,
    bytes_sha256,
    export_handoff_package,
)


def _trash_task_paused_before_index_write(
    root: str,
    box_name: str,
    reached_index_write,
    release_write,
) -> None:
    store = TrashStore(root)
    original_write_entries = store._write_entries

    def paused_write(entries):
        reached_index_write.set()
        if not release_write.wait(10):
            raise TimeoutError("timed out waiting to release trash write")
        original_write_entries(entries)

    store._write_entries = paused_write
    store.trash_task(box_name)


def _trash_task_and_signal(root: str, box_name: str, finished) -> None:
    TrashStore(root).trash_task(box_name)
    finished.set()


class TrashStoreTests(unittest.TestCase):
    def test_task_can_be_moved_to_trash_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "photo.jpg").write_bytes(b"photo")
            meta = {
                "__requirements__": [4, 5],
                box_name: {"4": [], "5": ["photo.jpg"], "__ocr__": {"size_type": "22G1"}},
            }
            (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

            store = TrashStore(root)
            entry = store.trash_task(box_name)

            self.assertFalse(box_dir.exists())
            self.assertEqual(store.list_entries()[0]["id"], entry["id"])
            deleted_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertNotIn(box_name, deleted_meta)

            store.restore(entry["id"])

            self.assertTrue((box_dir / "photo.jpg").is_file())
            restored_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(restored_meta[box_name]["__ocr__"]["size_type"], "22G1")
            self.assertEqual(store.list_entries(), [])

    def test_photo_can_be_moved_to_trash_and_restored_to_its_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            filename = "LYGU3043781-5.jpg"
            (box_dir / filename).write_bytes(b"photo")
            meta = {box_name: {"4": [], "5": [filename]}}
            (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

            store = TrashStore(root)
            entry = store.trash_photo(box_name, "5", filename)

            self.assertFalse((box_dir / filename).exists())
            self.assertEqual(entry["metadata_snapshot"], meta[box_name])
            deleted_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(deleted_meta[box_name]["5"], [])

            store.restore(entry["id"])

            self.assertEqual((box_dir / filename).read_bytes(), b"photo")
            restored_meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(restored_meta[box_name]["5"], [filename])

    def test_trash_entry_can_be_permanently_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / box_name / "photo.jpg").write_bytes(b"photo")
            (root / "meta.json").write_text(json.dumps({box_name: {}}), encoding="utf-8")
            store = TrashStore(root)
            entry = store.trash_task(box_name)
            stored_path = root / entry["storage_relpath"]

            store.purge(entry["id"])

            self.assertFalse(stored_path.exists())
            self.assertEqual(store.list_entries(), [])

    def test_purge_keeps_data_and_index_when_index_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / box_name / "photo.jpg").write_bytes(b"photo")
            (root / "meta.json").write_text(
                json.dumps({box_name: {}}),
                encoding="utf-8",
            )
            store = TrashStore(root)
            entry = store.trash_task(box_name)
            stored_path = root / entry["storage_relpath"]

            with patch.object(
                store,
                "_write_entries",
                side_effect=OSError("index is read only"),
            ):
                with self.assertRaises(OSError):
                    store.purge(entry["id"])

            self.assertTrue(stored_path.exists())
            self.assertEqual(
                [item["id"] for item in store.list_entries()],
                [entry["id"]],
            )

    def test_purge_does_not_restore_stale_index_when_data_deletion_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / "meta.json").write_text(
                json.dumps({box_name: {}}),
                encoding="utf-8",
            )
            store = TrashStore(root)
            entry = store.trash_task(box_name)
            stored_path = root / entry["storage_relpath"]

            with patch(
                "storage_services.shutil.rmtree",
                side_effect=OSError("data file is busy"),
            ):
                with self.assertRaises(OSError):
                    store.purge(entry["id"])

            self.assertTrue(stored_path.exists())
            self.assertEqual(store.list_entries(), [])

    def test_restore_refuses_name_conflict_without_losing_trash_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / box_name / "old.jpg").write_bytes(b"old")
            (root / "meta.json").write_text(json.dumps({box_name: {}}), encoding="utf-8")
            store = TrashStore(root)
            entry = store.trash_task(box_name)
            (root / box_name).mkdir()

            with self.assertRaises(FileExistsError):
                store.restore(entry["id"])

            self.assertTrue((root / entry["storage_relpath"]).exists())
            self.assertEqual(len(store.list_entries()), 1)

    def test_photo_restore_refuses_filename_already_referenced_in_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            filename = "door.jpg"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / filename).write_bytes(b"photo")
            (root / "meta.json").write_text(
                json.dumps({box_name: {"4": [filename], "5": []}}), encoding="utf-8"
            )
            store = TrashStore(root)
            entry = store.trash_photo(box_name, "4", filename)
            changed = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            changed[box_name]["5"] = [filename]
            (root / "meta.json").write_text(json.dumps(changed), encoding="utf-8")

            with self.assertRaises(FileExistsError):
                store.restore(entry["id"])

            self.assertTrue((root / entry["storage_relpath"]).is_file())
            self.assertEqual(len(store.list_entries()), 1)

    def test_trash_uses_last_good_metadata_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            snapshot = {"5": [], "__ocr__": {"size_type": "22G1"}}
            (root / "meta.json").write_text("{broken", encoding="utf-8")
            (root / "meta.json.bak").write_text(
                json.dumps({box_name: snapshot, "__requirements__": [5]}), encoding="utf-8"
            )

            entry = TrashStore(root).trash_task(box_name)

            self.assertEqual(entry["metadata_snapshot"], snapshot)

    def test_clear_removes_all_entries_only_when_explicitly_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BOX-A").mkdir()
            (root / "BOX-B").mkdir()
            (root / "meta.json").write_text(json.dumps({"BOX-A": {}, "BOX-B": {}}), encoding="utf-8")
            store = TrashStore(root)
            store.trash_task("BOX-A")
            store.trash_task("BOX-B")

            removed = store.clear()

            self.assertEqual(removed, 2)
            self.assertEqual(store.list_entries(), [])

    def test_clear_keeps_data_and_index_when_index_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_names = ("BOX-A", "BOX-B")
            for box_name in box_names:
                (root / box_name).mkdir()
            (root / "meta.json").write_text(
                json.dumps({box_name: {} for box_name in box_names}),
                encoding="utf-8",
            )
            store = TrashStore(root)
            entries = [store.trash_task(box_name) for box_name in box_names]
            stored_paths = [root / entry["storage_relpath"] for entry in entries]

            with patch.object(
                store,
                "_write_entries",
                side_effect=OSError("index is read only"),
            ):
                with self.assertRaises(OSError):
                    store.clear()

            self.assertTrue(all(path.exists() for path in stored_paths))
            self.assertEqual(
                {item["id"] for item in store.list_entries()},
                {entry["id"] for entry in entries},
            )

    def test_clear_does_not_restore_stale_index_when_data_deletion_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_names = ("BOX-A", "BOX-B")
            for box_name in box_names:
                (root / box_name).mkdir()
            (root / "meta.json").write_text(
                json.dumps({box_name: {} for box_name in box_names}),
                encoding="utf-8",
            )
            store = TrashStore(root)
            entries = [store.trash_task(box_name) for box_name in box_names]
            stored_paths = [root / entry["storage_relpath"] for entry in entries]

            with patch(
                "storage_services.shutil.rmtree",
                side_effect=OSError("data file is busy"),
            ):
                with self.assertRaises(OSError):
                    store.clear()

            self.assertTrue(all(path.exists() for path in stored_paths))
            self.assertEqual(store.list_entries(), [])

    def test_concurrent_process_trash_operations_keep_both_entries_and_metadata_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_names = ("BOX-A", "BOX-B")
            for box_name in box_names:
                (root / box_name).mkdir()
            (root / "meta.json").write_text(
                json.dumps({box_name: {} for box_name in box_names}),
                encoding="utf-8",
            )
            context = multiprocessing.get_context("spawn")
            first_reached_write = context.Event()
            release_first_write = context.Event()
            second_finished = context.Event()
            first = context.Process(
                target=_trash_task_paused_before_index_write,
                args=(
                    str(root),
                    box_names[0],
                    first_reached_write,
                    release_first_write,
                ),
            )
            second = context.Process(
                target=_trash_task_and_signal,
                args=(str(root), box_names[1], second_finished),
            )

            first.start()
            self.assertTrue(first_reached_write.wait(5))
            second.start()
            second_finished_before_release = second_finished.wait(1)
            release_first_write.set()
            first.join(10)
            second.join(10)

            self.assertFalse(second_finished_before_release)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))
            entries = TrashStore(root).list_entries()
            self.assertEqual(
                {entry["box_name"] for entry in entries},
                set(box_names),
            )
            remaining_meta = json.loads(
                (root / "meta.json").read_text(encoding="utf-8")
            )
            self.assertTrue(all(box_name not in remaining_meta for box_name in box_names))


class DuplicateDetectionTests(unittest.TestCase):
    def test_hash_index_finds_same_image_across_task_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            box_dir = Path(tmp)
            (box_dir / "first.jpg").write_bytes(b"same-image")
            (box_dir / "second.jpg").write_bytes(b"different-image")
            meta = {"4": ["first.jpg"], "5": ["second.jpg", "missing.jpg"]}

            index = build_image_hash_index(box_dir, meta)

            self.assertEqual(index[bytes_sha256(b"same-image")], "first.jpg")
            self.assertEqual(index[bytes_sha256(b"different-image")], "second.jpg")
            self.assertEqual(len(index), 2)


class HandoffExportTests(unittest.TestCase):
    def test_package_contains_summary_json_and_flat_container_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door-photo")
            meta = {
                "__requirements__": [1],
                box_name: {"1": ["door.jpg"], "__ocr__": {"size_type": "22G1"}},
                "MSCU6639870": {"1": []},
            }
            (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            destination = root / "handoff.zip"
            record = TaskExportRecord(
                name=box_name,
                size_type="22G1",
                ocr_status="match",
                task_status="已完成",
                photo_count=9,
                missing=(),
                last_updated="2026-07-10 10:00:00",
            )

            result = export_handoff_package(
                root,
                destination,
                [record],
                {1: "1 全开门空箱"},
            )

            self.assertEqual(result.task_count, 1)
            self.assertEqual(result.photo_count, 1)
            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                self.assertIn("summary.csv", names)
                self.assertIn("data.json", names)
                self.assertIn(f"{box_name}/", names)
                self.assertIn(f"{box_name}/door.jpg", names)
                self.assertFalse(any(name.startswith("containers/") for name in names))
                self.assertFalse(
                    any(
                        name.startswith(f"{box_name}/")
                        and not name.endswith("/")
                        and name.count("/") != 1
                        for name in names
                    )
                )
                csv_text = archive.read("summary.csv").decode("utf-8-sig")
                self.assertIn("箱号,箱型,OCR核对状态,任务状态,照片数,缺失项,最后更新时间", csv_text)
                self.assertIn(f"{box_name},22G1,match,已完成,1,,2026-07-10 10:00:00", csv_text)
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                self.assertEqual(exported["__requirements__"], [1])
                self.assertEqual(exported["__categories__"]["1"], "全开门空箱")
                self.assertEqual(exported["__export__"]["layout"], "箱号/照片")
                self.assertEqual(exported["__export__"]["task_count"], 1)
                self.assertEqual(exported["__export__"]["photo_count"], 1)
                self.assertIn(box_name, exported)
                self.assertNotIn("MSCU6639870", exported)
                self.assertEqual(exported[box_name]["1"], ["door.jpg"])
                self.assertEqual(exported[box_name]["__summary__"]["photo_count"], 1)
                archive_photos = {
                    name
                    for name in names
                    if name.startswith(f"{box_name}/") and not name.endswith("/")
                }
                json_photos = {
                    f"{box_name}/{filename}"
                    for category in map(str, range(1, 9))
                    for filename in exported[box_name][category]
                }
                self.assertEqual(archive_photos, json_photos)

    def test_empty_selected_task_keeps_its_container_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / "meta.json").write_text(
                json.dumps({"__requirements__": [1], box_name: {"1": []}}),
                encoding="utf-8",
            )
            destination = root / "empty.zip"
            record = TaskExportRecord(box_name, "", "", "待补", 0, ("全开门空箱",), "")

            export_handoff_package(root, destination, [record], {1: "1 全开门空箱"})

            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                self.assertIn(f"{box_name}/", names)
                self.assertFalse(
                    any(name.startswith(f"{box_name}/") and not name.endswith("/") for name in names)
                )
                self.assertEqual(exported[box_name]["1"], [])
                self.assertEqual(exported[box_name]["__summary__"]["photo_count"], 0)

    def test_all_scope_can_export_multiple_selected_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["LYGU3043781", "MSCU6639870"]
            meta = {"__requirements__": [1]}
            records = []
            for name in names:
                (root / name).mkdir()
                filename = f"{name}.jpg"
                (root / name / filename).write_bytes(name.encode("ascii"))
                meta[name] = {"1": [filename]}
                records.append(TaskExportRecord(name, "22G1", "match", "已完成", 1, (), ""))
            (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            destination = root / "all.zip"

            result = export_handoff_package(root, destination, records, {1: "1 全开门空箱"})

            self.assertEqual(result.task_count, 2)
            with zipfile.ZipFile(destination) as archive:
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                self.assertTrue(all(name in exported for name in names))

    def test_failed_export_removes_partial_file_and_keeps_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door-photo")
            (root / "meta.json").write_text(
                json.dumps({box_name: {"1": ["door.jpg"]}}), encoding="utf-8"
            )
            destination = root / "handoff.zip"
            destination.write_bytes(b"existing-package")
            record = TaskExportRecord(box_name, "", "", "待补", 1, (), "")

            def fail_progress(_current, _total, _label):
                raise RuntimeError("simulated export failure")

            with self.assertRaises(RuntimeError):
                export_handoff_package(root, destination, [record], {1: "分类"}, progress_callback=fail_progress)

            self.assertEqual(destination.read_bytes(), b"existing-package")
            self.assertEqual(
                [path.name for path in root.iterdir() if path.name.endswith(".partial")],
                [],
            )

    def test_partial_cleanup_error_does_not_hide_original_export_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door-photo")
            (root / "meta.json").write_text(
                json.dumps({box_name: {"1": ["door.jpg"]}}),
                encoding="utf-8",
            )
            destination = root / "handoff.zip"
            record = TaskExportRecord(box_name, "", "", "待补", 1, (), "")
            original_unlink = Path.unlink

            def fail_partial_unlink(path, *args, **kwargs):
                if path.name.endswith(".partial"):
                    raise PermissionError("partial file is busy")
                return original_unlink(path, *args, **kwargs)

            def fail_progress(_current, _total, _label):
                raise RuntimeError("original export failure")

            with patch.object(Path, "unlink", new=fail_partial_unlink), patch(
                "storage_services.log.warning"
            ) as cleanup_warning:
                with self.assertRaisesRegex(RuntimeError, "original export failure"):
                    export_handoff_package(
                        root,
                        destination,
                        [record],
                        {1: "分类"},
                        progress_callback=fail_progress,
                    )
            cleanup_warning.assert_called_once()

    def test_concurrent_exports_to_same_destination_do_not_share_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "door.jpg").write_bytes(b"door-photo")
            (root / "meta.json").write_text(
                json.dumps({box_name: {"1": ["door.jpg"]}}),
                encoding="utf-8",
            )
            destination = root / "handoff.zip"
            record = TaskExportRecord(box_name, "", "", "待补", 1, (), "")
            both_archives_open = threading.Barrier(2)
            failed_export_done = threading.Event()
            successful_progress_started = threading.Event()
            results = {}
            errors = {}

            def progress_for_failed_export(_current, _total, _label):
                both_archives_open.wait(timeout=5)
                raise RuntimeError("simulated concurrent export failure")

            def progress_for_successful_export(_current, _total, _label):
                if successful_progress_started.is_set():
                    return
                successful_progress_started.set()
                both_archives_open.wait(timeout=5)
                if not failed_export_done.wait(timeout=5):
                    raise TimeoutError("failed export did not finish cleanup")

            def run_export(label, progress_callback):
                try:
                    results[label] = export_handoff_package(
                        root,
                        destination,
                        [record],
                        {1: "分类"},
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    errors[label] = exc
                finally:
                    if label == "failed":
                        failed_export_done.set()

            failed_thread = threading.Thread(
                target=run_export,
                args=("failed", progress_for_failed_export),
            )
            successful_thread = threading.Thread(
                target=run_export,
                args=("successful", progress_for_successful_export),
            )
            failed_thread.start()
            successful_thread.start()
            failed_thread.join(timeout=10)
            successful_thread.join(timeout=10)

            self.assertFalse(failed_thread.is_alive())
            self.assertFalse(successful_thread.is_alive())
            self.assertIsInstance(errors.get("failed"), RuntimeError)
            self.assertNotIn("successful", errors)
            self.assertIn("successful", results)
            with zipfile.ZipFile(destination) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn(f"{box_name}/door.jpg", archive.namelist())
            self.assertEqual(
                [path.name for path in root.iterdir() if path.name.endswith(".partial")],
                [],
            )

    def test_export_transformer_updates_photo_names_and_size_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            original = b"large-png-payload"
            (box_dir / "door.png").write_bytes(original)
            (root / "meta.json").write_text(
                json.dumps({box_name: {"1": ["door.png"]}}), encoding="utf-8"
            )
            destination = root / "optimized.zip"
            record = TaskExportRecord(box_name, "22G1", "match", "已完成", 1, (), "")

            result = export_handoff_package(
                root,
                destination,
                [record],
                {1: "1 全开门空箱"},
                image_transformer=lambda _source: ("door.jpg", b"jpeg"),
            )

            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                self.assertIn(f"{box_name}/door.jpg", names)
                self.assertNotIn(f"{box_name}/door.png", names)
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                self.assertEqual(exported[box_name]["1"], ["door.jpg"])
            self.assertEqual(result.optimized_photo_count, 1)
            self.assertEqual(result.source_photo_bytes, len(original))
            self.assertEqual(result.exported_photo_bytes, len(b"jpeg"))

    def test_flat_export_deduplicates_stale_metadata_and_renames_transformed_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            box_dir = root / box_name
            box_dir.mkdir()
            (box_dir / "first.png").write_bytes(b"first")
            (box_dir / "second.bmp").write_bytes(b"second")
            (root / "meta.json").write_text(
                json.dumps(
                    {
                        "__requirements__": [1, 2, 3],
                        box_name: {
                            "1": ["first.png"],
                            "2": ["second.bmp"],
                            "3": ["first.png"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            destination = root / "collisions.zip"
            record = TaskExportRecord(box_name, "", "", "待补", 3, (), "")

            result = export_handoff_package(
                root,
                destination,
                [record],
                {
                    1: "1 全开门空箱",
                    2: "2 全开门1/2箱",
                    3: "3 全开门满箱",
                },
                image_transformer=lambda source: ("photo.jpg", source.read_bytes()),
            )

            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                exported = json.loads(archive.read("data.json").decode("utf-8"))
                self.assertIn(f"{box_name}/photo.jpg", names)
                self.assertIn(f"{box_name}/photo_2.jpg", names)
                self.assertEqual(exported[box_name]["1"], ["photo.jpg"])
                self.assertEqual(exported[box_name]["2"], ["photo_2.jpg"])
                self.assertEqual(exported[box_name]["3"], [])
                self.assertEqual(exported[box_name]["__summary__"]["photo_count"], 2)
                self.assertEqual(
                    exported[box_name]["__summary__"]["missing"],
                    ["全开门满箱"],
                )
                self.assertEqual(exported["__export__"]["photo_count"], 2)
            self.assertEqual(result.photo_count, 2)
            self.assertEqual(result.optimized_photo_count, 2)

    def test_cancelled_export_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box_name = "LYGU3043781"
            (root / box_name).mkdir()
            (root / "meta.json").write_text(json.dumps({box_name: {}}), encoding="utf-8")
            destination = root / "handoff.zip"
            cancelled = threading.Event()
            cancelled.set()
            record = TaskExportRecord(box_name, "", "", "待补", 0, ("关门",), "")

            with self.assertRaises(ExportCancelled):
                export_handoff_package(root, destination, [record], {}, cancelled)

            self.assertFalse(destination.exists())
            self.assertEqual(
                [path.name for path in root.iterdir() if path.name.endswith(".partial")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
