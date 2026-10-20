import json
import logging
import os
import re
import shutil
import struct
import sys
import time
import threading
from collections import OrderedDict
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QSize, Signal, QPoint, QRect, QEvent, QRunnable, QThreadPool, QObject, QTimer, QBuffer, QIODevice, QByteArray, QUrl
from PySide6.QtGui import QIcon, QColor, QBrush, QPainter, QPen, QImage, QImageReader, QPixmap, QKeySequence, QShortcut, QDesktopServices, QPalette, QCursor, QFontMetrics
from PySide6.QtWidgets import *

from storage_services import (
    ExportCancelled,
    TaskExportRecord,
    TrashStore,
    build_image_hash_index,
    bytes_sha256,
    export_handoff_package,
    file_sha256,
)
from report_services import (
    OCR_STATUS_LABELS,
    TASK_STATUS_LABELS,
    CollectionReport,
    ReportExportCancelled,
    build_collection_report,
    export_collection_report_xlsx,
)
from task_services import (
    ExportPreflight,
    TaskSnapshot,
    _infer_app_named_category,
    build_export_preflight,
    resolve_task_photos,
)
from health_services import HealthReport, HealthScanCancelled, scan_data_health
from metadata_services import read_metadata, update_metadata, write_metadata
from image_import_services import (
    ImageImportSource,
    ImageImportSummary,
    import_task_images,
)
from export_naming import (
    DEFAULT_EXPORT_TEMPLATES,
    render_export_filename,
)
from photo_classifier import (
    MANUAL as AUTO_SORT_MANUAL,
    PLACE as AUTO_SORT_PLACE,
    PhotoClassifier,
    placement_for,
)
from theme_services import (
    DEFAULT_THEME_NAME,
    THEME_PALETTES,
    get_theme_palette,
    normalize_theme_name,
)
from ocr_services import (
    current_ocr_result,
    BatchAnalysis,
    BatchCandidate,
    analyze_batch_input,
    annotate_ocr_verification,
    box_center,
    build_container_number_hint,
    build_container_candidates,
    classify_task_state,
    extract_declared_container_no,
    important_ocr_complete,
    iso6346_check_digit,
    merge_important_ocr_results,
    normalize_container_candidate,
    normalize_ocr_rows,
    normalize_task_name,
    parse_capacity,
    parse_container_ocr,
    parse_size_type,
    parse_weight_value,
    parse_weights,
    read_boxed_check_digit,
    validate_container_no,
)
from updater_services import (
    DownloadCancelled,
    InstallPlan,
    UpdateCheckResult,
    UpdateError,
    UpdateManifest,
    check_for_update,
    cleanup_successful_update_backup,
    consume_post_update_health_args,
    consume_update_failure,
    discard_install_plan,
    download_update,
    launch_install_helper,
    prepare_install_plan,
    watch_update_notifications,
    write_post_update_health,
)
from authorization_service import AuthStatus, AuthorizationDecision, AuthorizationService

try:
    import qtawesome as qta
except ImportError:
    qta = None

log = logging.getLogger(__name__)


def configure_file_logging() -> Path:
    local_appdata = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    log_dir = local_appdata / "PhotoCollect" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(handler, RotatingFileHandler) for handler in log.handlers):
        handler = RotatingFileHandler(
            log_dir / "app.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log_dir


LOG_DIR = configure_file_logging()

# ---------- Configuration & Constants ----------
APPDATA_DIR = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
CONFIG_PATH = APPDATA_DIR / "container_picker" / "config.json"
META_NAME = "meta.json"
META_BAK_NAME = "meta.json.bak"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}
APP_LOGO_PNG = "assets/app-logo.png"
APP_LOGO_ICO = "assets/app-logo.ico"
APP_DISPLAY_NAME = "集装箱照片收集工具"
APP_VERSION = "5.16.20"
OCR_REGRESSION_CONTAINER_NO = "LYGU3043781"
OCR_REGRESSION_SIZE_TYPE = "22G1"
OCR_REGRESSION_FIXTURE = "tests/fixtures/container_sample.jpg"

WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WINDOWS_SNAP_STYLE = WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
HTCAPTION = 2
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

ALL_CATEGORIES = [
    (1, "1 全开门空箱"), (2, "2 全开门1/2箱"), (3, "3 全开门满箱"), (4, "4 半关门满箱"),
    (5, "5 全关门"), (6, "6 加固/底梁 1"), (7, "7 加固/底梁 2"), (8, "8 铅封/其他")
]

DEFAULT_REQS = [1, 2, 3, 4, 5, 6, 7, 8]
AUTO_OCR_CATEGORIES = {4, 5}
CLIPBOARD_JPEG_QUALITY = 88
STORED_IMAGE_MAX_EDGE = 2560
_ocr_engine = None
_ocr_lock = threading.Lock()
_ocr_inference_lock = threading.Lock()


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


_photo_classifier = None
_photo_classifier_loaded = False


def get_photo_classifier():
    """The bundled slot classifier, or None when models/photo_classifier.onnx is absent."""
    global _photo_classifier, _photo_classifier_loaded
    if not _photo_classifier_loaded:
        try:
            _photo_classifier = PhotoClassifier.from_directory(resource_path("models"))
        except Exception:
            log.exception("Unable to load the photo classifier")
            _photo_classifier = None
        _photo_classifier_loaded = True
    return _photo_classifier


def qimage_jpeg_bytes(image: QImage, quality: int = CLIPBOARD_JPEG_QUALITY) -> bytes:
    """Encode a photo-sized QImage as an opaque, high-quality JPEG."""
    if image.isNull():
        return b""
    prepared = QImage(image)
    longest_edge = max(prepared.width(), prepared.height())
    if longest_edge > STORED_IMAGE_MAX_EDGE:
        prepared = prepared.scaled(
            STORED_IMAGE_MAX_EDGE,
            STORED_IMAGE_MAX_EDGE,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    if prepared.hasAlphaChannel():
        flattened = QImage(prepared.size(), QImage.Format_RGB32)
        flattened.fill(QColor("white"))
        painter = QPainter(flattened)
        painter.drawImage(0, 0, prepared)
        painter.end()
        prepared = flattened
    else:
        prepared = prepared.convertToFormat(QImage.Format_RGB888)

    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.WriteOnly):
        return b""
    ok = prepared.save(buffer, "JPEG", quality)
    buffer.close()
    return bytes(data) if ok else b""


def optimize_export_image(source: Path) -> tuple[str, bytes] | None:
    """Convert legacy lossless photo files to compact JPEGs inside handoff ZIPs."""
    if source.suffix.lower() not in {".png", ".bmp"}:
        return None
    image = QImage(str(source))
    payload = qimage_jpeg_bytes(image)
    if not payload:
        return None
    return source.with_suffix(".jpg").name, payload


# ---------- Utility Functions ----------
def read_meta(path: Path) -> dict:
    """Read and parse meta.json, falling back to meta.json.bak when possible."""
    return read_metadata(path, path.with_name(META_BAK_NAME))


def write_meta(path: Path, data: dict) -> None:
    """Write data to meta.json atomically and keep a last-good backup."""
    write_metadata(path, data, path.with_name(META_BAK_NAME))


def update_meta(path: Path, mutator) -> dict:
    """Apply one metadata update while holding the cross-process write lock."""
    return update_metadata(path, mutator, path.with_name(META_BAK_NAME))


def is_remote_work_root(path: Path | str) -> bool:
    path = Path(path)
    value = str(path)
    if value.startswith(("\\\\", "//")):
        return True
    if sys.platform != "win32" or not path.anchor:
        return False
    try:
        import ctypes

        drive_type_remote = 4
        return ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == drive_type_remote
    except (AttributeError, OSError):
        return False


def probe_work_root(path: Path | str) -> tuple[bool, str]:
    try:
        if Path(path).is_dir():
            return True, ""
        return False, "目录不存在或当前无法访问"
    except OSError as exc:
        return False, str(exc)


def _sanitize_filename(name: str, fallback: str = "dropped-image.png") -> str:
    clean = re.sub(r'[\\/:*?"<>|]', '', Path(name).name.strip())
    return clean or fallback


def _parse_file_group_descriptor(data: bytes, wide: bool = True) -> list:
    if len(data) < 4:
        return []
    try:
        count = struct.unpack_from("<I", data, 0)[0]
    except struct.error:
        return []
    if count <= 0 or count > 100:
        return []

    descriptor_size = 592 if wide else 332
    name_offset = 72
    name_size = 520 if wide else 260
    names = []
    for i in range(count):
        offset = 4 + i * descriptor_size
        if offset + name_offset + name_size > len(data):
            break
        raw_name = data[offset + name_offset:offset + name_offset + name_size]
        if wide:
            name = raw_name.decode("utf-16le", errors="ignore").split("\x00", 1)[0]
        else:
            name = raw_name.decode("mbcs", errors="ignore").split("\x00", 1)[0]
        names.append(_sanitize_filename(name, f"dropped-image-{i + 1}.png"))
    return names


def _windows_mime_format(mime, value: str, index: int = None) -> str:
    prefix = f'application/x-qt-windows-mime;value="{value}"'
    formats = mime.formats()
    if index is not None:
        exact = f"{prefix};index={index}"
        if exact in formats:
            return exact
    for fmt in formats:
        if not fmt.startswith(prefix):
            continue
        if index is None or f";index={index}" in fmt:
            return fmt
    return ""


def extract_windows_virtual_images(mime) -> list:
    """Extract Windows OLE virtual image files such as FileGroupDescriptor/FileContents."""
    descriptor_fmt = _windows_mime_format(mime, "FileGroupDescriptorW")
    wide = True
    if not descriptor_fmt:
        descriptor_fmt = _windows_mime_format(mime, "FileGroupDescriptor")
        wide = False
    if not descriptor_fmt:
        return []

    descriptor = bytes(mime.data(descriptor_fmt))
    names = _parse_file_group_descriptor(descriptor, wide=wide)
    if not names:
        return []

    images = []
    for i, name in enumerate(names):
        content_fmt = _windows_mime_format(mime, "FileContents", i)
        if not content_fmt:
            content_fmt = _windows_mime_format(mime, "FileContents")
        if not content_fmt:
            continue
        payload = bytes(mime.data(content_fmt))
        if not payload:
            continue
        ext = Path(name).suffix.lower()
        if ext in IMAGE_EXTENSIONS or QImage.fromData(payload).isNull() is False:
            images.append((name, payload))
    return images


def accepts_image_drag(mime) -> bool:
    return bool(
        mime.hasUrls()
        or mime.hasImage()
        or _windows_mime_format(mime, "FileGroupDescriptorW")
        or _windows_mime_format(mime, "FileGroupDescriptor")
    )


def read_image_drop(mime):
    """Classify a drop as ("files", paths), ("virtual", [(name, bytes)]), ("image", data),
    ("bad_files", None) for non-image files, or ("unsupported", None)."""
    files = [
        url.toLocalFile() for url in mime.urls()
        if os.path.isfile(url.toLocalFile()) and Path(url.toLocalFile()).suffix.lower() in IMAGE_EXTENSIONS
    ]
    if files:
        return "files", files
    virtual_images = extract_windows_virtual_images(mime)
    if virtual_images:
        return "virtual", virtual_images
    if mime.hasImage():
        return "image", mime.imageData()
    if mime.hasUrls():
        return "bad_files", None
    return "unsupported", None


def build_task_summary(box_dir: Path, box_meta: dict, global_reqs, require_ocr: bool = True) -> dict:
    """Summarize one task directory without touching dashboard widgets."""
    data = box_meta if isinstance(box_meta, dict) else {}
    photo_state = resolve_task_photos(
        box_dir,
        data,
        global_reqs,
        category_count=len(ALL_CATEGORIES),
        auto_ocr_categories=AUTO_OCR_CATEGORIES,
    )
    requirements = photo_state.required_categories

    ocr_data = data.get("__ocr__", {})
    if not isinstance(ocr_data, dict):
        ocr_data = {}
    ocr_data = current_ocr_result(ocr_data, box_dir.name, box_dir)
    ocr_container = ocr_data.get("container_no", "")
    ocr_size_type = ocr_data.get("size_type", "")
    ocr_status = ocr_data.get("verification_status", "")
    if ocr_data and not ocr_status:
        ocr_status = annotate_ocr_verification(ocr_data, box_dir.name).get("verification_status", "")

    category_labels = dict(ALL_CATEGORIES)
    missing_category_ids = list(photo_state.missing_category_ids)
    missing = []
    for index in missing_category_ids:
        category_label = category_labels.get(index, str(index))
        missing.append(category_label.split(" ", 1)[1] if " " in category_label else category_label)
    photo_count = photo_state.photo_count
    has_auto_ocr_photo = photo_state.has_auto_ocr_photo

    task_state = classify_task_state(
        box_dir.name,
        missing,
        ocr_status,
        has_auto_ocr_photo,
        require_ocr=require_ocr,
    )
    declared_number = extract_declared_container_no(box_dir.name)
    box_number_valid = bool(declared_number and validate_container_no(declared_number))
    try:
        updated_ts = box_dir.stat().st_mtime
    except OSError:
        updated_ts = 0.0
    last_updated = datetime.fromtimestamp(updated_ts).strftime("%Y-%m-%d %H:%M:%S") if updated_ts else ""
    return TaskSnapshot(
        name=box_dir.name,
        size_type=ocr_size_type,
        ocr_container=ocr_container,
        ocr_status=ocr_status,
        task_state=task_state,
        photo_count=photo_count,
        source_photo_bytes=photo_state.source_photo_bytes,
        required_categories=requirements,
        missing_category_ids=photo_state.missing_category_ids,
        missing_labels=tuple(missing),
        category_counts=photo_state.category_counts,
        updated_ts=updated_ts,
        last_updated=last_updated,
        box_number_valid=box_number_valid,
        has_auto_ocr_photo=has_auto_ocr_photo,
        ocr_enabled=bool(require_ocr),
    ).to_summary()


def scan_task_summaries(root: Path, require_ocr: bool = True) -> dict:
    """Read one work-root snapshot without touching Qt widgets."""
    root = Path(root)
    meta = read_meta(root / META_NAME)
    loaded_reqs = meta.get("__requirements__", DEFAULT_REQS)
    reqs = loaded_reqs if isinstance(loaded_reqs, list) else DEFAULT_REQS
    directories = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ),
        key=lambda path: path.name,
    )
    task_data = []
    ok_count = 0
    state_counts = {
        "all": len(directories),
        "pending": 0,
        "anomaly": 0,
        "complete": 0,
    }
    for directory in directories:
        summary = build_task_summary(
            directory,
            meta.get(directory.name, {}),
            reqs,
            require_ocr=require_ocr,
        )
        task_data.append(summary)
        state_counts[summary["task_state"]] += 1
        if summary["ok"]:
            ok_count += 1
    return {
        "task_data": task_data,
        "ok_count": ok_count,
        "state_counts": state_counts,
    }


def format_file_size(value: int) -> str:
    size = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def ensure_ocr_engine():
    """Create the shared OCR engine once; safe to call from any thread."""
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        return _ocr_engine


def run_ocr_image(image_path: str) -> dict:
    """Run the shared OCR engine and return the parsed container result."""
    t0 = time.time()
    engine = ensure_ocr_engine()
    with _ocr_inference_lock:
        # Load once so the framed check digit can be re-read from the same pixels.
        image = engine.load_img(image_path)
        raw_result, _ = engine(image)

        def read_check_digit(serial_bounds):
            try:
                return read_boxed_check_digit(engine, image, serial_bounds)
            except Exception:
                log.exception("Check-digit re-read failed")
                return None

        result = parse_container_ocr(raw_result, check_digit_reader=read_check_digit)
    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


def run_ocr_smoke_cli(argv) -> int:
    """Headless packaged-app OCR probe used by release verification."""
    import traceback

    output_path = Path(argv[argv.index("--ocr-output") + 1]) if "--ocr-output" in argv else None
    payload = {"ok": False}
    try:
        image_path = Path(argv[argv.index("--ocr-smoke") + 1])
        if not image_path.is_file():
            raise FileNotFoundError(f"OCR test image does not exist: {image_path}")
        result = run_ocr_image(str(image_path))
        container_no = str(result.get("container_no", ""))
        size_type = str(result.get("size_type", ""))
        check_digit_valid = bool(result.get("check_digit_valid"))
        matches_expected = (
            container_no == OCR_REGRESSION_CONTAINER_NO
            and size_type == OCR_REGRESSION_SIZE_TYPE
            and check_digit_valid
        )
        payload = {
            "ok": matches_expected,
            "container_no": container_no,
            "size_type": size_type,
            "check_digit_valid": check_digit_valid,
            "elapsed_ms": result.get("elapsed_ms", 0),
            "expected": {
                "container_no": OCR_REGRESSION_CONTAINER_NO,
                "size_type": OCR_REGRESSION_SIZE_TYPE,
                "check_digit_valid": True,
            },
        }
        if not matches_expected:
            payload["error"] = (
                "OCR regression mismatch: "
                f"expected {OCR_REGRESSION_CONTAINER_NO}/{OCR_REGRESSION_SIZE_TYPE} "
                f"with a valid check digit, got {container_no or '<empty>'}/"
                f"{size_type or '<empty>'} (check_digit_valid={check_digit_valid})"
            )
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if payload["ok"] else 1


def run_acceptance_smoke_cli(argv) -> int:
    from acceptance_workflow import run_acceptance

    if QApplication.instance() is None:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    def option_path(name: str) -> Path | None:
        if name not in argv:
            return None
        index = argv.index(name) + 1
        if index >= len(argv):
            raise ValueError(f"{name} 缺少路径参数")
        return Path(argv[index])

    try:
        output = option_path("--acceptance-output")
        if output is None:
            output = Path.cwd() / "outputs" / "acceptance-smoke.json"
        workspace = option_path("--acceptance-workspace")
        fixture = option_path("--ocr-fixture")
        if fixture is None:
            fixture = resource_path(OCR_REGRESSION_FIXTURE)
        return run_acceptance(
            sys.modules[__name__],
            output,
            workspace=workspace,
            ocr_fixture=fixture,
        )
    except Exception as exc:
        log.exception("Acceptance smoke setup failed")
        output = Path.cwd() / "outputs" / "acceptance-smoke-setup-error.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1


class OcrSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


def run_bound_ocr(image_path):
    path = Path(image_path)
    before = path.stat()
    digest = file_sha256(path)
    result = run_ocr_image(str(path))
    after = path.stat()
    signature = [after.st_size, after.st_mtime_ns, after.st_ctime_ns]
    if signature != [before.st_size, before.st_mtime_ns, before.st_ctime_ns] or file_sha256(path) != digest:
        raise OSError('照片在识别过程中发生变化，请重新识别')
    result.update(source_image=path.name, source_images=[path.name],
                  source_sha256={path.name: digest}, source_signatures={path.name: signature})
    return result


class OcrTask(QRunnable):
    def __init__(self, image_path: str):
        super().__init__()
        self.image_path = image_path
        self.signals = OcrSignals()

    def run(self):
        try:
            self.signals.finished.emit(run_bound_ocr(self.image_path))
        except ModuleNotFoundError as e:
            missing = e.name or "未知模块"
            self.signals.error.emit(f"OCR 组件不完整，缺少模块：{missing}")
        except Exception as e:
            log.exception("OCR failed")
            self.signals.error.emit(str(e))


class UpdateCheckSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class UpdateCheckTask(QRunnable):
    """Fetch the release pointer without ever blocking the Qt event loop."""

    def __init__(self, current_version: str, *, manual: bool = False):
        super().__init__()
        self.current_version = current_version
        self.manual = bool(manual)
        self.signals = UpdateCheckSignals()
        self.setAutoDelete(True)

    def run(self):
        try:
            self.signals.finished.emit(check_for_update(self.current_version))
        except Exception as exc:
            log.info("Update check failed: %s", exc)
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                pass


class AuthorizationSignals(QObject):
    finished = Signal(object)


class AuthorizationTask(QRunnable):
    """Run the silent lease check away from the Qt event loop."""

    def __init__(self, service: AuthorizationService, app_version: str):
        super().__init__()
        self.service = service
        self.app_version = app_version
        self.signals = AuthorizationSignals()
        self.setAutoDelete(True)

    def run(self):
        try:
            decision = self.service.check(self.app_version)
        except Exception as exc:  # fail closed when authorization is enabled
            log.exception("Authorization check failed")
            decision = AuthorizationDecision(AuthStatus.INVALID, False, str(exc))
        try:
            self.signals.finished.emit(decision)
        except RuntimeError:
            pass


class UpdateDownloadSignals(QObject):
    progress = Signal(int, int)
    finished = Signal(object)
    cancelled = Signal()
    error = Signal(str)


class UpdateDownloadTask(QRunnable):
    """Download, verify, and prepare a one-file update in the background."""

    def __init__(self, manifest: UpdateManifest, target_executable: Path):
        super().__init__()
        self.manifest = manifest
        self.target_executable = Path(target_executable)
        self.cancel_event = threading.Event()
        self.signals = UpdateDownloadSignals()
        self.setAutoDelete(True)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            staged_path = download_update(
                self.manifest,
                cancel_event=self.cancel_event,
                timeout=10.0,
                progress_callback=lambda current, total: self.signals.progress.emit(
                    current,
                    total,
                ),
            )
            if self.cancel_event.is_set():
                raise DownloadCancelled("更新下载已取消")
            plan = prepare_install_plan(
                self.manifest,
                staged_path,
                target_executable=self.target_executable,
            )
            if self.cancel_event.is_set():
                discard_install_plan(plan)
                raise DownloadCancelled("更新下载已取消")
            self.signals.finished.emit(plan)
        except DownloadCancelled:
            try:
                self.signals.cancelled.emit()
            except RuntimeError:
                pass
        except Exception as exc:
            log.exception("Background update download failed")
            try:
                self.signals.error.emit(str(exc))
            except RuntimeError:
                pass


class UpdateNotificationBridge(QObject):
    release_ready = Signal(object)


class ExportSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    cancelled = Signal()
    error = Signal(str)


class WorkRootProbeSignals(QObject):
    finished = Signal(int, str, bool, str)


class TaskScanSignals(QObject):
    finished = Signal(int, str, object, str)


class TaskScanTask(QRunnable):
    """Build a task-list snapshot without blocking the Qt event loop."""

    def __init__(
        self,
        generation: int,
        root: Path,
        require_ocr: bool,
    ):
        super().__init__()
        self.generation = int(generation)
        self.root = Path(root)
        self.require_ocr = bool(require_ocr)
        self.signals = TaskScanSignals()
        self.setAutoDelete(True)

    def run(self):
        snapshot = None
        error = ""
        try:
            snapshot = scan_task_summaries(
                self.root,
                require_ocr=self.require_ocr,
            )
        except Exception as exc:
            log.exception("Unable to scan work directory %s", self.root)
            error = str(exc)
        try:
            self.signals.finished.emit(
                self.generation,
                str(self.root),
                snapshot,
                error,
            )
        except RuntimeError:
            pass


class HealthCheckSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    cancelled = Signal()
    error = Signal(str)


class HealthCheckTask(QRunnable):
    def __init__(self, root: Path, validator, photo_classifier=None):
        super().__init__()
        self.root = Path(root)
        self.validator = validator
        self.photo_classifier = photo_classifier
        self.cancel_event = threading.Event()
        self.signals = HealthCheckSignals()
        self.setAutoDelete(True)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = scan_data_health(
                self.root,
                task_name_validator=self.validator,
                cancel_event=self.cancel_event,
                progress_callback=lambda current, total, label: self.signals.progress.emit(
                    current,
                    total,
                    label,
                ),
                photo_classifier=self.photo_classifier,
                category_labels={idx: name.split(" ", 1)[-1] for idx, name in ALL_CATEGORIES},
            )
            self.signals.finished.emit(report)
        except HealthScanCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            log.exception("Data health check failed")
            self.signals.error.emit(str(exc))


class ExportTask(QRunnable):
    def __init__(self, root, destination, records, category_names, image_transformer=None):
        super().__init__()
        self.root = root
        self.destination = destination
        self.records = records
        self.category_names = category_names
        self.image_transformer = image_transformer
        self.cancel_event = threading.Event()
        self.signals = ExportSignals()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            summary = export_handoff_package(
                self.root,
                self.destination,
                self.records,
                self.category_names,
                self.cancel_event,
                lambda current, total, label: self.signals.progress.emit(current, total, label),
                self.image_transformer,
            )
            self.signals.finished.emit(summary)
        except ExportCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            log.exception("Handoff export failed")
            self.signals.error.emit(str(exc))


class ExcelExportTask(QRunnable):
    def __init__(self, report, destination, work_root):
        super().__init__()
        self.report = report
        self.destination = destination
        self.work_root = work_root
        self.cancel_event = threading.Event()
        self.signals = ExportSignals()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            output = export_collection_report_xlsx(
                self.report,
                self.destination,
                self.work_root,
                cancel_event=self.cancel_event,
                progress_callback=lambda current, total, label: self.signals.progress.emit(
                    current,
                    total,
                    label,
                ),
            )
            self.signals.finished.emit(output)
        except ReportExportCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            log.exception("Collection report export failed")
            self.signals.error.emit(str(exc))


class ImageImportSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    error = Signal(str)


class ImageImportTask(QRunnable):
    """Prepare, hash, write, and commit one image batch outside the UI thread."""

    def __init__(self, root_dir: Path, box_name: str, category: int, sources: list):
        super().__init__()
        self.root_dir = Path(root_dir)
        self.box_name = str(box_name)
        self.category = int(category)
        self.sources = list(sources)
        self.cancel_event = threading.Event()
        self.signals = ImageImportSignals()
        self.setAutoDelete(True)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        prepared = []
        preparation_failures = []
        try:
            for index, source in enumerate(self.sources, start=1):
                if self.cancel_event.is_set():
                    break
                kind = source[0]
                label = str(source[1])
                self.signals.progress.emit(index, len(self.sources), label)
                if kind == "path":
                    prepared.append(ImageImportSource.from_path(source[2]))
                    continue
                if kind == "payload":
                    raw_payload = bytes(source[2])
                    source_digest = bytes_sha256(raw_payload)
                    image = QImage.fromData(raw_payload)
                elif kind == "qimage":
                    source_digest = ""
                    image = QImage(source[2])
                else:
                    preparation_failures.append(f"{label}: 不支持的图片来源")
                    continue
                if image.isNull():
                    preparation_failures.append(f"{label}: 无法解码图片")
                    continue
                compact_payload = qimage_jpeg_bytes(image)
                if not compact_payload:
                    preparation_failures.append(f"{label}: 无法编码为 JPEG")
                    continue
                prepared.append(
                    ImageImportSource.from_payload(
                        label,
                        compact_payload,
                        extension=".jpg",
                        source_digest=source_digest,
                    )
                )

            summary = import_task_images(
                self.root_dir,
                self.box_name,
                self.category,
                prepared,
                cancel_event=self.cancel_event,
                progress_callback=lambda current, total, label: self.signals.progress.emit(
                    current,
                    total,
                    label,
                ),
            )
            if preparation_failures:
                summary = ImageImportSummary(
                    added_names=summary.added_names,
                    duplicate_names=summary.duplicate_names,
                    duplicate_count=summary.duplicate_count,
                    skipped_count=summary.skipped_count,
                    failed_count=summary.failed_count + len(preparation_failures),
                    cancelled=summary.cancelled or self.cancel_event.is_set(),
                    failures=summary.failures + tuple(preparation_failures),
                )
            self.signals.finished.emit(summary)
        except Exception as exc:
            log.exception("Background image import failed")
            self.signals.error.emit(str(exc))


@dataclass(frozen=True)
class AutoSortItem:
    source: tuple
    prediction: object = None
    duplicate_of: str = ""
    error: str = ""

    @property
    def label(self) -> str:
        return str(self.source[1])


def _predict_first_readable(classifier, samples):
    error = None
    for sample in samples:
        try:
            return classifier.predict(sample)
        except Exception as exc:
            error = exc
    raise error


def classify_import_sources(classifier, box_dir, box_meta, sources) -> list:
    """Predict a slot for each drop source (same tuples as ImageImportTask).

    Photos already saved in the task are reported as duplicates instead of being
    classified.  Payload and QImage sources are hashed as the JPEG the importer
    would save; payloads are classified from their original bytes so EXIF
    orientation is honoured.
    """
    hash_index = build_image_hash_index(box_dir, box_meta)
    items = []
    for source in sources:
        kind = source[0]
        try:
            if kind == "path":
                digest = file_sha256(source[2])
                samples = (source[2],)
            elif kind in ("payload", "qimage"):
                raw = bytes(source[2]) if kind == "payload" else b""
                image = QImage.fromData(raw) if kind == "payload" else QImage(source[2])
                compact = qimage_jpeg_bytes(image) if not image.isNull() else b""
                if not compact:
                    raise ValueError("无法解码图片")
                digest = bytes_sha256(compact)
                samples = (raw, compact) if raw else (compact,)
            else:
                raise ValueError("不支持的图片来源")
            duplicate = hash_index.get(digest, "")
            if duplicate:
                items.append(AutoSortItem(source, duplicate_of=duplicate))
                continue
            hash_index[digest] = str(source[1])
            items.append(AutoSortItem(source, prediction=_predict_first_readable(classifier, samples)))
        except Exception as exc:
            log.warning("Auto sort could not read %s: %s", source[1], exc)
            items.append(AutoSortItem(source, error=str(exc)))
    return items


class PhotoClassifySignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class PhotoClassifyTask(QRunnable):
    """Run the slot classifier for one drop outside the UI thread."""

    def __init__(self, classifier, box_dir: Path, box_meta: dict, sources: list):
        super().__init__()
        self.classifier = classifier
        self.box_dir = Path(box_dir)
        self.box_meta = json.loads(json.dumps(box_meta))
        self.sources = list(sources)
        self.signals = PhotoClassifySignals()
        self.setAutoDelete(True)

    def run(self):
        try:
            self.signals.finished.emit(
                classify_import_sources(self.classifier, self.box_dir, self.box_meta, self.sources)
            )
        except Exception as exc:
            log.exception("Photo classification failed")
            self.signals.error.emit(str(exc))


class BatchOcrSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    error = Signal(str)


class BatchOcrRunner(QRunnable):
    """Recognize only important fields from queued door-photo task snapshots."""

    def __init__(self, pending_tasks: list, root_dir: Path):
        super().__init__()
        self.pending_tasks = list(pending_tasks)
        self.root_dir = Path(root_dir)
        self.cancel_event = threading.Event()
        self.signals = BatchOcrSignals()
        self.setAutoDelete(True)

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        success_count = 0
        error_count = 0
        image_count = 0
        updated_tasks = []
        total = len(self.pending_tasks)
        try:
            for index, task_info in enumerate(self.pending_tasks, start=1):
                if self.cancel_event.is_set():
                    break
                box_name = str(task_info["name"])
                image_paths = [Path(path) for path in task_info.get("image_paths", [])]
                self.signals.progress.emit(
                    index,
                    total,
                    f"正在识别 {box_name}（{index}/{total}）",
                )
                try:
                    results = []
                    for image_path in image_paths:
                        if self.cancel_event.is_set():
                            break
                        result = run_bound_ocr(image_path)
                        result["source_image"] = image_path.name
                        result["source_images"] = [image_path.name]
                        results.append(result)
                        image_count += 1
                    if not results:
                        if not self.cancel_event.is_set():
                            error_count += 1
                        continue
                    merged = merge_important_ocr_results(results)
                    checked = annotate_ocr_verification(
                        merged,
                        box_name,
                        automatic=False,
                    )

                    def apply_ocr_update(all_data):
                        box_data = all_data.get(box_name, {})
                        if not isinstance(box_data, dict):
                            box_data = {}
                        valid = current_ocr_result(checked, box_name, self.root_dir / box_name)
                        pending_source = box_data.get('__ocr__', {}).get('pending_source')
                        if (pending_source and pending_source not in checked.get('source_images', [])):
                            raise OSError('新的关门照片已保存，旧批量识别结果未提交')
                        if valid.get('verification_reason') and not checked.get('conflicting_evidence'):
                            raise OSError('OCR 来源照片已变化，结果未保存')
                        box_data["__ocr__"] = valid
                        all_data[box_name] = box_data

                    update_meta(self.root_dir / META_NAME, apply_ocr_update)
                    success_count += 1
                    updated_tasks.append(box_name)
                except Exception:
                    log.exception("Batch OCR failed for %s", box_name)
                    error_count += 1
            self.signals.finished.emit({
                "success_count": success_count,
                "error_count": error_count,
                "image_count": image_count,
                "updated_tasks": tuple(updated_tasks),
                "cancelled": self.cancel_event.is_set(),
            })
        except Exception as exc:
            log.exception("Batch OCR runner failed")
            self.signals.error.emit(str(exc))


# ---------- Thumbnail Cache ----------
class ThumbnailCache:
    """Thread-safe QImage-only LRU bounded by both item count and memory."""

    def __init__(self, max_size: int = 200, max_bytes: int = 64 * 1024 * 1024):
        self._cache: OrderedDict = OrderedDict()
        self._max = max(1, int(max_size))
        self._max_bytes = max(1, int(max_bytes))
        self._bytes_used = 0
        self._lock = threading.RLock()

    @staticmethod
    def _image_bytes(image: QImage) -> int:
        try:
            return max(0, int(image.sizeInBytes()))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return max(0, image.width() * image.height() * 4)

    def _remove_key(self, key) -> None:
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._bytes_used = max(0, self._bytes_used - entry[1])

    def _remove_stale_path_entries(self, path: str, size: QSize, keep_key=None) -> None:
        dimensions = (size.width(), size.height())
        for key in list(self._cache):
            if (
                key != keep_key
                and key[0] == path
                and key[2:] == dimensions
            ):
                self._remove_key(key)

    def _evict_to(self, max_items: int, max_bytes: int) -> None:
        while self._cache and (
            len(self._cache) > max_items or self._bytes_used > max_bytes
        ):
            key = next(iter(self._cache))
            self._remove_key(key)

    def get(self, path: str, size: QSize) -> Optional[QImage]:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = (path, mtime, size.width(), size.height())
        with self._lock:
            self._remove_stale_path_entries(path, size, keep_key=key)
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._cache.move_to_end(key)
            return entry[0]

    def put(self, path: str, size: QSize, image: QImage) -> None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        key = (path, mtime, size.width(), size.height())
        image_bytes = self._image_bytes(image)
        with self._lock:
            self._remove_stale_path_entries(path, size, keep_key=key)
            self._remove_key(key)
            self._cache[key] = (image, image_bytes)
            self._bytes_used += image_bytes
            self._cache.move_to_end(key)
            self._evict_to(self._max, self._max_bytes)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes_used

    def trim_to_fraction(self, fraction: float = 0.5) -> None:
        fraction = min(1.0, max(0.0, float(fraction)))
        target_items = max(1, int(self._max * fraction))
        target_bytes = max(1, int(self._max_bytes * fraction))
        with self._lock:
            self._evict_to(target_items, target_bytes)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._bytes_used = 0


_thumb_cache = ThumbnailCache()


# ---------- Modern Design System (Graphite Dark) ----------
class Theme:
    NAME = DEFAULT_THEME_NAME
    # Colors — graphite dark palette with electric-blue accent
    BG_MAIN = "#1E2024"        # app background (graphite)
    BG_SIDEBAR = "#17191D"     # sidebar (deeper graphite)
    BG_CARD = "#26292F"        # cards / inputs
    BG_SUBTLE = "#2C2F36"      # subtle fills
    BG_ACTIVE = "#363A42"      # hover / active
    PRIMARY = "#5C8DFF"        # electric blue accent
    PRIMARY_HOVER = "#4678E8"  # accent hover
    PRIMARY_SOFT = "#2C3B59"   # soft blue tint on dark
    TEXT_MAIN = "#E8EAED"      # near-white ink
    TEXT_SEC = "#A9AEB6"
    TEXT_MUTED = "#7E838B"
    TEXT_INVERT = "#FFFFFF"
    BORDER = "#33373E"
    BORDER_HOVER = "#454A53"
    SUCCESS = "#4CC38A"
    SUCCESS_SOFT = "#20312A"
    SUCCESS_LINE = "#2F4A3C"
    ERROR = "#F27065"
    ERROR_SOFT = "#392A28"
    ERROR_LINE = "#55352F"
    WARNING = "#E5B566"
    WARNING_SOFT = "#373023"
    WARNING_LINE = "#514227"
    INK_ELEVATED = "#0E0F11"   # darkest surfaces (toast / tooltip)
    INK_BORDER = "#2E3238"
    ACCENT_ON_DARK = "#8FB2FF" # action text on darkest surfaces
    TEXT_ON_INK = "#F2F3F5"    # body text on darkest surfaces
    SCROLL_HANDLE = "#4A4E56"
    SCROLL_HANDLE_HOVER = "#5A5F68"

    # Fonts
    FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
    FONT_FAMILY_BRAND = 'Georgia, "Times New Roman", "Songti SC", serif'
    FONT_SIZE_MAIN = "14px"
    FONT_SIZE_TITLE = "18px"
    
    # Stylesheet
    STYLESHEET = f"""
    QMainWindow, QDialog {{ background-color: {BG_MAIN}; }}
    QWidget {{
        font-family: {FONT_FAMILY};
        font-size: {FONT_SIZE_MAIN};
        color: {TEXT_MAIN};
    }}
    
    /* Cards */
    QFrame.Card {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QFrame.Panel {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QToolTip {{
        background-color: {INK_ELEVATED};
        color: {TEXT_ON_INK};
        border: none;
        padding: 6px 8px;
        border-radius: 4px;
        font-size: 12px;
    }}

    /* Buttons */
    QPushButton {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 7px;
        padding: 7px 11px;
        color: {TEXT_MAIN};
        font-weight: 600;
    }}
    QPushButton:hover {{
        background-color: {BG_SUBTLE};
        border-color: {BORDER_HOVER};
        color: {TEXT_MAIN};
    }}
    QPushButton:focus {{
        border-color: {PRIMARY};
    }}
    QPushButton[class="Primary"] {{
        background-color: {PRIMARY};
        color: white;
        border: none;
    }}
    QPushButton[class="Primary"]:hover {{
        background-color: {PRIMARY_HOVER};
    }}
    QPushButton[class="Primary"]:focus {{
        background-color: {PRIMARY_HOVER};
    }}
    QPushButton[class="Ghost"] {{
        background-color: transparent;
        border: 1px solid transparent;
        color: {TEXT_MAIN};
    }}
    QPushButton[class="Ghost"]:hover {{
        background-color: {BG_ACTIVE};
        border-color: transparent;
        color: {TEXT_MAIN};
    }}
    QPushButton[class="Ghost"]:focus {{
        border-color: {PRIMARY};
    }}
    QPushButton[class="Danger"] {{
        background-color: {ERROR_SOFT};
        color: {ERROR};
        border: 1px solid {ERROR_LINE};
    }}
    QToolButton:focus {{
        border: 1px solid {PRIMARY};
    }}

    /* Inputs */
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 7px;
        padding: 7px;
        selection-background-color: {PRIMARY};
        selection-color: white;
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
        border: 1px solid {PRIMARY};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 4px;
        outline: none;
        selection-background-color: {BG_ACTIVE};
        selection-color: {TEXT_MAIN};
    }}

    /* Trees & shared table headers */
    QTreeWidget {{
        background-color: {BG_CARD};
        alternate-background-color: {BG_SUBTLE};
        border: 1px solid {BORDER};
        border-radius: 8px;
    }}
    QHeaderView::section {{
        background: {BG_SUBTLE};
        color: {TEXT_SEC};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 7px 8px;
        font-size: 12px;
        font-weight: 700;
    }}
    
    /* List Widget */
    QListWidget {{
        background-color: transparent;
        border: none;
        outline: none;
    }}
    QListWidget::item {{
        border-radius: 7px;
        padding: 0px;
        margin: 2px 0px;
    }}
    QListWidget::item:selected {{
        background-color: {BG_ACTIVE};
        color: {TEXT_MAIN};
    }}
    QListWidget::item:hover {{
        background-color: {BG_SUBTLE};
    }}
    
    /* Scrollbars */
    QScrollBar:vertical {{
        border: none;
        background: transparent;
        width: 8px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {SCROLL_HANDLE};
        min-height: 20px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {SCROLL_HANDLE_HOVER};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        border: none;
        background: transparent;
        height: 8px;
        margin: 0px;
    }}
    QScrollBar::handle:horizontal {{
        background: {SCROLL_HANDLE};
        min-width: 20px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {SCROLL_HANDLE_HOVER};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}
    QProgressBar {{
        background: {BORDER};
        border: none;
        border-radius: 3px;
    }}
    QProgressBar::chunk {{
        background: {PRIMARY};
        border-radius: 3px;
    }}
    QCheckBox {{ spacing: 8px; }}
    """


MENU_STYLESHEET = f"""
    QMenu {{
        background-color: {Theme.BG_MAIN};
        border: 1px solid {Theme.BORDER};
        border-radius: 7px;
        padding: 4px;
    }}
    QMenu::item {{
        min-height: 20px;
        padding: 6px 24px 6px 10px;
        margin: 1px 0;
        border-radius: 5px;
        color: {Theme.TEXT_MAIN};
    }}
    QMenu::item:selected {{
        background-color: {Theme.BG_ACTIVE};
        color: {Theme.TEXT_MAIN};
    }}
    QMenu::item:disabled {{
        color: {Theme.TEXT_MUTED};
    }}
    QMenu::separator {{
        height: 1px;
        background: {Theme.BORDER};
        margin: 4px 8px;
    }}
"""


_THEME_BASE_VALUES = {
    key: getattr(Theme, key)
    for key in get_theme_palette(DEFAULT_THEME_NAME).colors
}
_THEME_BASE_STYLESHEET = Theme.STYLESHEET
_THEME_BASE_MENU_STYLESHEET = MENU_STYLESHEET


def apply_theme(theme_name: str) -> str:
    """Apply one semantic palette before constructing application widgets."""
    global MENU_STYLESHEET
    palette = get_theme_palette(theme_name)
    stylesheet = _THEME_BASE_STYLESHEET
    menu_stylesheet = _THEME_BASE_MENU_STYLESHEET
    for key, base_value in _THEME_BASE_VALUES.items():
        selected_value = palette.colors[key]
        setattr(Theme, key, selected_value)
        stylesheet = stylesheet.replace(base_value, selected_value)
        menu_stylesheet = menu_stylesheet.replace(base_value, selected_value)
    Theme.NAME = palette.name
    Theme.STYLESHEET = stylesheet
    MENU_STYLESHEET = menu_stylesheet
    icon_cache = globals().get("_drawn_icon_cache")
    if isinstance(icon_cache, dict):
        icon_cache.clear()
    return palette.name


def load_saved_theme_name() -> str:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return DEFAULT_THEME_NAME
    if not isinstance(payload, dict):
        return DEFAULT_THEME_NAME
    return normalize_theme_name(payload.get("theme_name", DEFAULT_THEME_NAME))


def configure_app_style(app: "QApplication", theme_name: str | None = None) -> None:
    """Apply Fusion plus the selected semantic palette."""
    if theme_name is not None:
        apply_theme(theme_name)
    app.setStyle("Fusion")
    palette = app.palette()
    roles = {
        QPalette.Window: Theme.BG_MAIN,
        QPalette.WindowText: Theme.TEXT_MAIN,
        QPalette.Base: Theme.BG_CARD,
        QPalette.AlternateBase: Theme.BG_SUBTLE,
        QPalette.Text: Theme.TEXT_MAIN,
        QPalette.Button: Theme.BG_MAIN,
        QPalette.ButtonText: Theme.TEXT_MAIN,
        QPalette.Highlight: Theme.PRIMARY,
        QPalette.HighlightedText: Theme.TEXT_INVERT,
        QPalette.ToolTipBase: Theme.INK_ELEVATED,
        QPalette.ToolTipText: Theme.TEXT_ON_INK,
        QPalette.PlaceholderText: Theme.TEXT_MUTED,
        QPalette.Link: Theme.PRIMARY_HOVER,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(Theme.TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(Theme.TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(Theme.TEXT_MUTED))
    app.setPalette(palette)
    app.setStyleSheet(Theme.STYLESHEET)


ICON_MAP = {
    "back": ("fa5s.arrow-left", QStyle.SP_ArrowBack),
    "folder": ("fa5s.folder-open", QStyle.SP_DirOpenIcon),
    "add": ("fa5s.plus", QStyle.SP_FileDialogNewFolder),
    "settings": ("fa5s.cog", QStyle.SP_FileDialogDetailedView),
    "export": ("fa5s.file-export", QStyle.SP_DialogSaveButton),
    "refresh": ("fa5s.sync-alt", QStyle.SP_BrowserReload),
    "sort": ("fa5s.sort-amount-down", QStyle.SP_ArrowDown),
    "open": ("fa5s.external-link-alt", QStyle.SP_DialogOpenButton),
    "delete": ("fa5s.trash", QStyle.SP_TrashIcon),
    "image": ("fa5s.image", QStyle.SP_FileIcon),
    "view": ("fa5s.search-plus", QStyle.SP_FileDialogContentsView),
    "replace": ("fa5s.exchange-alt", QStyle.SP_DialogResetButton),
    "ok": ("fa5s.check-circle", QStyle.SP_DialogApplyButton),
    "warn": ("fa5s.exclamation-triangle", QStyle.SP_MessageBoxWarning),
    "collapse": ("fa5s.angle-left", QStyle.SP_ArrowLeft),
    "expand": ("fa5s.angle-right", QStyle.SP_ArrowRight),
    "more": ("fa5s.ellipsis-h", QStyle.SP_TitleBarMenuButton),
    "chart": ("fa5s.chart-bar", QStyle.SP_FileDialogInfoView),
    "copy": ("fa5s.copy", QStyle.SP_DialogOpenButton),
}

_drawn_icon_cache = {}


def app_icon(widget: QWidget, name: str, color: str | None = None) -> QIcon:
    color = color or Theme.TEXT_MAIN
    fa_name, fallback = ICON_MAP.get(name, ("fa5s.circle", QStyle.SP_FileIcon))
    if qta is not None:
        try:
            return qta.icon(fa_name, color=color)
        except Exception:
            pass
    return drawn_icon(name, color)


def drawn_icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    color = color or Theme.TEXT_MAIN
    cache_key = (name, color, int(size))
    cached = _drawn_icon_cache.get(cache_key)
    if cached is not None:
        return QIcon(cached)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = size
    c = s / 2

    if name == "back":
        p.drawLine(12, 5, 7, 10)
        p.drawLine(7, 10, 12, 15)
    elif name == "folder":
        p.drawRoundedRect(3, 6, 14, 10, 2, 2)
        p.drawLine(4, 6, 8, 6)
        p.drawLine(8, 6, 10, 8)
    elif name == "add":
        p.drawLine(c, 5, c, 15)
        p.drawLine(5, c, 15, c)
    elif name == "settings":
        p.drawEllipse(7, 7, 6, 6)
        for x1, y1, x2, y2 in [(10, 3, 10, 5), (10, 15, 10, 17), (3, 10, 5, 10), (15, 10, 17, 10), (5, 5, 6.5, 6.5), (13.5, 13.5, 15, 15), (15, 5, 13.5, 6.5), (5, 15, 6.5, 13.5)]:
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
    elif name == "export":
        p.drawRoundedRect(4, 4, 12, 12, 2, 2)
        p.drawLine(10, 7, 10, 13)
        p.drawLine(7, 10, 10, 13)
        p.drawLine(13, 10, 10, 13)
    elif name == "refresh":
        p.drawArc(4, 4, 12, 12, 30 * 16, 260 * 16)
        p.drawLine(14, 5, 16, 5)
        p.drawLine(16, 5, 16, 3)
    elif name == "sort":
        p.drawLine(6, 5, 14, 5)
        p.drawLine(8, 10, 14, 10)
        p.drawLine(10, 15, 14, 15)
    elif name == "open":
        p.drawRoundedRect(4, 6, 10, 10, 2, 2)
        p.drawLine(10, 4, 16, 4)
        p.drawLine(16, 4, 16, 10)
        p.drawLine(16, 4, 9, 11)
    elif name == "delete":
        p.drawLine(6, 7, 14, 7)
        p.drawRoundedRect(7, 8, 6, 8, 1, 1)
        p.drawLine(8, 5, 12, 5)
    elif name == "image":
        p.drawRoundedRect(4, 5, 12, 10, 2, 2)
        p.drawEllipse(7, 8, 2, 2)
        p.drawLine(5, 14, 9, 10)
        p.drawLine(9, 10, 12, 13)
        p.drawLine(12, 13, 15, 10)
    elif name == "view":
        p.drawEllipse(4, 7, 12, 7)
        p.drawEllipse(8, 8, 4, 4)
    elif name == "replace":
        p.drawLine(5, 7, 14, 7)
        p.drawLine(14, 7, 11, 4)
        p.drawLine(14, 7, 11, 10)
        p.drawLine(15, 13, 6, 13)
        p.drawLine(6, 13, 9, 10)
        p.drawLine(6, 13, 9, 16)
    elif name == "ok":
        p.drawLine(5, 10, 9, 14)
        p.drawLine(9, 14, 15, 6)
    elif name == "warn":
        p.drawLine(10, 4, 17, 16)
        p.drawLine(17, 16, 3, 16)
        p.drawLine(3, 16, 10, 4)
        p.drawLine(10, 8, 10, 12)
        p.drawPoint(10, 14)
    elif name == "collapse":
        p.drawLine(12, 5, 7, 10)
        p.drawLine(7, 10, 12, 15)
    elif name == "expand":
        p.drawLine(8, 5, 13, 10)
        p.drawLine(13, 10, 8, 15)
    elif name == "more":
        for x in (5, 10, 15):
            p.drawEllipse(x - 1, 9, 2, 2)
    elif name == "chart":
        p.drawLine(4, 16, 17, 16)
        p.drawRoundedRect(5, 10, 2, 5, 1, 1)
        p.drawRoundedRect(9, 6, 2, 9, 1, 1)
        p.drawRoundedRect(13, 3, 2, 12, 1, 1)
    elif name == "copy":
        p.drawRoundedRect(7, 5, 9, 10, 2, 2)
        p.drawRoundedRect(4, 8, 9, 9, 2, 2)
    elif name == "search":
        p.drawEllipse(5, 5, 8, 8)
        p.drawLine(12, 12, 16, 16)
    elif name == "minimize":
        p.drawLine(5, 13, 15, 13)
    elif name == "maximize":
        p.drawRoundedRect(5, 5, 10, 10, 1, 1)
    elif name == "restore":
        p.drawRoundedRect(7, 5, 8, 8, 1, 1)
        p.drawRoundedRect(5, 7, 8, 8, 1, 1)
    elif name == "close":
        p.drawLine(6, 6, 14, 14)
        p.drawLine(14, 6, 6, 14)
    elif name == "about":
        p.drawEllipse(5, 5, 10, 10)
        p.drawPoint(10, 8)
        p.drawLine(10, 10, 10, 14)
    else:
        p.drawEllipse(5, 5, 10, 10)
    p.end()
    icon = QIcon(pm)
    _drawn_icon_cache[cache_key] = icon
    return QIcon(icon)


def style_button(button: QPushButton, role: str = "Secondary", icon_name: str = None, tooltip: str = None) -> QPushButton:
    if role != "Secondary":
        button.setProperty("class", role)
    button.setCursor(Qt.PointingHandCursor)
    button.setMinimumHeight(34)
    if icon_name:
        if role == "Primary":
            icon_color = "white"
        elif role == "Danger":
            icon_color = Theme.ERROR
        else:
            icon_color = Theme.TEXT_SEC
        button.setIcon(app_icon(button, icon_name, icon_color))
        button.setIconSize(QSize(16, 16))
    if tooltip:
        button.setToolTip(tooltip)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


def style_command_button(button: QToolButton, text_button: bool = False) -> QToolButton:
    """Apply one compact Codex-like treatment to command-bar tool buttons."""
    horizontal_padding = 10 if text_button else 7
    button.setCursor(Qt.PointingHandCursor)
    button.setAutoRaise(False)
    button.setMinimumHeight(34)
    button.setIconSize(QSize(16, 16))
    button.setStyleSheet(f"""
        QToolButton {{
            background: {Theme.BG_CARD};
            color: {Theme.TEXT_MAIN};
            border: 1px solid {Theme.BORDER};
            border-radius: 7px;
            padding: 5px {horizontal_padding}px;
            font-size: 12px;
            font-weight: 600;
        }}
        QToolButton:hover {{
            background: {Theme.BG_SUBTLE};
            border-color: {Theme.BORDER_HOVER};
        }}
        QToolButton:pressed, QToolButton:checked {{
            background: {Theme.BG_ACTIVE};
            border-color: #4E535C;
        }}
        QToolButton:focus {{
            border-color: {Theme.PRIMARY};
        }}
        QToolButton:disabled {{
            color: {Theme.TEXT_MUTED};
            background: {Theme.BG_SUBTLE};
        }}
        QToolButton::menu-indicator {{
            image: none;
            width: 0px;
            height: 0px;
        }}
    """)
    return button


def styled_menu(parent=None) -> QMenu:
    menu = QMenu(parent) if parent is not None else QMenu()
    menu.setStyleSheet(MENU_STYLESHEET)
    return menu


def style_sidebar_button(button: QPushButton, collapsed: bool = False) -> QPushButton:
    """Keep sidebar actions aligned and unclipped in both sidebar widths."""
    button.setCursor(Qt.PointingHandCursor)
    button.setMinimumHeight(34)
    button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    if collapsed:
        button.setFixedWidth(32)
        alignment = "center"
        padding = "0px"
    else:
        button.setMinimumWidth(0)
        button.setMaximumWidth(16777215)
        alignment = "left"
        padding = "7px 9px"
    button.setStyleSheet(f"""
        QPushButton {{
            color: {Theme.TEXT_MAIN};
            text-align: {alignment};
            padding: {padding};
            border: none;
            border-radius: 7px;
            background: transparent;
        }}
        QPushButton:hover {{ background: {Theme.BG_ACTIVE}; }}
        QPushButton:pressed {{ background: #3E434B; }}
        QPushButton:checked {{ background: {Theme.BG_ACTIVE}; font-weight: 700; }}
        QPushButton:focus {{ background: {Theme.BG_ACTIVE}; }}
    """)
    return button


def set_solid_background(widget: QWidget, color: str) -> None:
    palette = widget.palette()
    solid = QColor(color)
    palette.setColor(QPalette.Window, solid)
    palette.setColor(QPalette.Base, solid)
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


class SolidColorViewport(QWidget):
    """Opaque scroll-area viewport that does not depend on platform palette behavior."""

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._background_color = QColor(color)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self._background_color)


class ElidedLabel(QLabel):
    """Single-line label that never forces its row wider than the available space."""

    def __init__(self, text: str = "", parent=None, auto_tooltip: bool = True):
        super().__init__(parent)
        self._full_text = ""
        self._auto_tooltip = bool(auto_tooltip)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def fullText(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self._update_elided_text()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        width = max(0, self.contentsRect().width())
        rendered = QFontMetrics(self.font()).elidedText(
            self._full_text,
            Qt.ElideRight,
            width,
        )
        QLabel.setText(self, rendered)
        if self._auto_tooltip:
            self.setToolTip(self._full_text if rendered != self._full_text else "")


class ToggleSwitch(QAbstractButton):
    """Compact accessible switch for sidebar binary settings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(36, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self.rect().adjusted(1, 2, -1, -2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(Theme.PRIMARY if self.isChecked() else "#4A4E56"))
        painter.drawRoundedRect(track, 9, 9)

        knob_size = 14
        knob_x = track.right() - knob_size - 2 if self.isChecked() else track.left() + 2
        knob = QRect(knob_x, track.top() + 2, knob_size, knob_size)
        painter.setBrush(QColor("white"))
        painter.drawEllipse(knob)

        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(Theme.PRIMARY), 1))
            painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)


def apply_status_badge(label: QLabel, text: str, status: str, compact: bool = False) -> QLabel:
    colors = {
        "ok": (Theme.SUCCESS, Theme.SUCCESS_SOFT, Theme.SUCCESS_LINE),
        "warn": (Theme.WARNING, Theme.WARNING_SOFT, Theme.WARNING_LINE),
        "bad": (Theme.ERROR, Theme.ERROR_SOFT, Theme.ERROR_LINE),
        "info": (Theme.PRIMARY, Theme.PRIMARY_SOFT, Theme.BORDER_HOVER),
        "neutral": (Theme.TEXT_SEC, Theme.BG_SUBTLE, Theme.BORDER),
    }
    fg, bg, border = colors.get(status, colors["neutral"])
    height = 24 if compact else 28
    horizontal_padding = 7 if compact else 9
    label.setText(text)
    label.setAlignment(Qt.AlignCenter)
    label.setFixedHeight(height)
    label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    label.setStyleSheet(
        f"color:{fg}; background:{bg}; border:1px solid {border}; "
        f"font-weight:700; font-size:12px; padding:0px {horizontal_padding}px; border-radius:6px;"
    )
    return label


def status_badge(text: str, status: str, compact: bool = False) -> QLabel:
    return apply_status_badge(QLabel(), text, status, compact)


def build_requirements_checklist(current_reqs, parent=None):
    """Build the shared eight-category checklist used by both requirement dialogs."""
    content = QWidget(parent)
    content.setStyleSheet("background: transparent;")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    checks = {}
    selected = set(current_reqs) if isinstance(current_reqs, list) else set(DEFAULT_REQS)
    for idx, name in ALL_CATEGORIES:
        checkbox = QCheckBox(name)
        checkbox.setChecked(idx in selected)
        layout.addWidget(checkbox)
        checks[idx] = checkbox
    layout.addStretch()
    return content, checks


class WindowControlButton(QPushButton):
    """Title-bar control that stays neutral until hovered (danger turns red)."""

    def __init__(self, icon_name: str, tooltip: str, danger: bool = False, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._danger = bool(danger)
        self.setFixedSize(34, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setIconSize(QSize(15, 15))
        self.set_control_icon(icon_name)
        hover_bg = "#C0392B" if self._danger else Theme.BG_ACTIVE
        self.setStyleSheet(f"""
            QPushButton {{
                border: none;
                border-radius: 7px;
                background: transparent;
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {hover_bg};
            }}
        """)

    def set_control_icon(self, icon_name: str, hovered: bool = False) -> None:
        self._icon_name = icon_name
        if self._danger and hovered:
            color = "white"
        else:
            color = Theme.TEXT_SEC
        self.setIcon(app_icon(self, icon_name, color))

    def enterEvent(self, event):
        if self._danger:
            self.set_control_icon(self._icon_name, hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._danger:
            self.set_control_icon(self._icon_name, hovered=False)
        super().leaveEvent(event)


def window_button(parent: QWidget, icon_name: str, tooltip: str, danger: bool = False) -> QPushButton:
    return WindowControlButton(icon_name, tooltip, danger)

# ---------- Helper: Flow Layout ----------
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, h_spacing=10, v_spacing=10):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def horizontalSpacing(self):
        return self._h_spacing

    def verticalSpacing(self):
        return self._v_spacing

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    @staticmethod
    def _effective_item_size(item):
        size = item.sizeHint()
        widget = item.widget()
        if widget is None:
            return size
        minimum = widget.minimumSize()
        maximum = widget.maximumSize()
        size.setWidth(max(minimum.width(), min(size.width(), maximum.width())))
        size.setHeight(max(minimum.height(), min(size.height(), maximum.height())))
        return size

    def do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        spacing_x = self.horizontalSpacing()
        spacing_y = self.verticalSpacing()

        for item in self._items:
            item_size = self._effective_item_size(item)
            space_x = spacing_x
            space_y = spacing_y
            next_x = x + item_size.width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + item_size.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y() + margins.bottom()

# ---------- Components ----------
class DropZoneCard(QFrame):
    files_dropped = Signal(int, list)
    image_dropped = Signal(int, object)
    virtual_files_dropped = Signal(int, list)
    selected = Signal(int)

    def __init__(self, idx, name, is_required, parent=None):
        super().__init__(parent)
        self.setObjectName("DropZoneCard")
        self.idx = idx
        self.name = name
        self.is_required = is_required
        self.state = "empty"
        self.is_selected = False
        self._visual_state_key = None
        self._last_card_width = None
        self._last_card_height = None
        self._photo_area = (200, 150)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._card_layout = QVBoxLayout(self)
        self._card_layout.setContentsMargins(12, 12, 12, 12)
        self._card_layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        header.setSpacing(6)
        self.lbl_title = QLabel(self.name)
        self.lbl_title.setWordWrap(True)
        header.addWidget(self.lbl_title)

        self.req_badge = status_badge("必填", "neutral")
        header.addWidget(self.req_badge)

        header.addStretch()
        self._card_layout.addLayout(header)

        # Image List - 自适应尺寸
        self.lw = QListWidget()
        self.lw.setViewMode(QListWidget.IconMode)
        self.lw.setResizeMode(QListWidget.Adjust)
        self.lw.setIconSize(QSize(200, 150))
        self.lw.setGridSize(QSize(200, 150))
        self.lw.setSpacing(4)
        self.lw.setMovement(QListWidget.Static)
        self.lw.setStyleSheet("background: transparent; border: none;")
        self.lw.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._card_layout.addWidget(self.lw)

        # Add Button
        self.btn_add = QPushButton("添加图片")
        self.btn_add.setToolTip("选择图片；也可以从 Telegram 拖入图片，或复制图片后点击卡片按 Ctrl+V")
        style_button(self.btn_add, icon_name="add")
        self.btn_add.setFixedHeight(32)
        self.btn_add.clicked.connect(self.trigger)
        self._card_layout.addWidget(self.btn_add)

        for target in (self.lbl_title, self.req_badge, self.lw.viewport()):
            target.installEventFilter(self)

        self.set_card_width(240, force=True)
        self.set_required(is_required, refresh_width=False)

    def set_required(self, required: bool, refresh_width: bool = True) -> None:
        self.is_required = required
        apply_status_badge(
            self.req_badge,
            "必填" if required else "选填",
            "warn" if required else "neutral",
            compact=True,
        )
        self.req_badge.show()
        if refresh_width:
            self.set_card_width(self.width() or 240, force=True)
        self.set_state(self.state)

    def set_state(self, state: str) -> None:
        self.state = state
        visual_key = (state, self.is_required, self.is_selected)
        if visual_key == self._visual_state_key:
            return
        self._visual_state_key = visual_key
        if self.is_selected:
            border, bg, width = Theme.PRIMARY, Theme.BG_CARD, 2
        elif not self.is_required:
            border, bg = Theme.BORDER, Theme.BG_SUBTLE
            width = 1
        elif state == "complete":
            border, bg = Theme.SUCCESS_LINE, Theme.BG_CARD
            width = 1
        else:
            border, bg = Theme.WARNING_LINE, Theme.WARNING_SOFT
            width = 1
        self.setStyleSheet(f"""
            DropZoneCard {{
                background-color: {bg};
                border: {width}px solid {border};
                border-radius: 8px;
            }}
            DropZoneCard:hover {{
                border: {width}px solid {Theme.PRIMARY};
                background-color: {Theme.BG_CARD};
            }}
        """)

    def set_selected(self, selected: bool) -> None:
        if self.is_selected == selected:
            return
        self.is_selected = selected
        self.set_state(self.state)

    @staticmethod
    def _default_style() -> str:
        return f"""
            DropZoneCard {{
                background-color: {Theme.BG_CARD};
                border: 1px solid {Theme.BORDER};
                border-radius: 8px;
            }}
            DropZoneCard:hover {{
                border: 1px solid {Theme.PRIMARY};
                background-color: {Theme.BG_CARD};
            }}
        """

    @staticmethod
    def _drag_style() -> str:
        return f"""
            DropZoneCard {{
                background-color: {Theme.BG_SUBTLE};
                border: 2px dashed {Theme.PRIMARY};
                border-radius: 8px;
            }}
        """

    def set_card_width(self, w, force=False):
        self.set_card_size(w, None, force=force)

    def set_card_size(self, w, h=None, force=False):
        """Fix the card width and, when `h` is given, its height too (the
        detail page uses that to fit all eight cards on screen); otherwise the
        photo area keeps a 4:3 shape."""
        w = int(w)
        h = int(h) if h else None
        if not force and self._last_card_width == w and self._last_card_height == h:
            return
        self._last_card_width = w
        self._last_card_height = h
        self.setFixedWidth(w)
        pad = max(9, min(14, w // 22))
        spacing = 8
        self._card_layout.setContentsMargins(pad, pad, pad, pad)
        self._card_layout.setSpacing(spacing)

        self.lbl_title.setStyleSheet(f"""
            font-weight: 700; font-size: 13px;
            color: {Theme.TEXT_MAIN if self.is_required else Theme.TEXT_SEC};
        """)

        btn_h = 32
        # 图片列表自适应
        lw_w = max(80, w - pad * 2)
        if h:
            header_h = max(self.lbl_title.sizeHint().height(), self.req_badge.sizeHint().height())
            lw_h = max(60, h - pad * 2 - header_h - btn_h - spacing * 2)
        else:
            lw_h = max(60, int(lw_w * 0.75))
        self.lw.setFixedSize(lw_w, lw_h)
        self._photo_area = (lw_w, lw_h)
        self.apply_photo_grid()
        self.btn_add.setFixedHeight(btn_h)
        self.btn_add.setStyleSheet(f"""
            QPushButton {{
                border: 1px dashed {Theme.BORDER_HOVER};
                color: {Theme.TEXT_SEC};
                background: {Theme.BG_CARD};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover, QPushButton:focus {{
                border: 1px dashed {Theme.PRIMARY};
                color: {Theme.PRIMARY};
                background: {Theme.PRIMARY_SOFT};
            }}
        """)

        if h:
            self.setFixedHeight(h)
        else:
            self.setMaximumHeight(16777215)
            card_min_h = max(120, lw_h + btn_h + pad * 2 + 30)
            self.setMinimumHeight(card_min_h)

    def photo_columns(self) -> int:
        """Photos shown side by side: as many portrait-shaped cells as fit."""
        lw_w, lw_h = self._photo_area
        # A portrait photo needs about 3/4 of the height in width; a cell a
        # little narrower only shrinks the photo slightly.
        fit = max(1, round(lw_w / max(80, lw_h * 0.75)))
        return max(1, min(self.lw.count(), fit))

    def apply_photo_grid(self) -> None:
        lw_w, lw_h = self._photo_area
        count = self.lw.count()
        columns = self.photo_columns()
        # More photos than columns scroll vertically; keep room for the bar.
        scrolls = count > columns
        # Without this a scroll bar left over from an earlier layout narrows
        # the view and pushes the last photo of a full row onto a new row.
        self.lw.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if scrolls else Qt.ScrollBarAlwaysOff)
        scrollbar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent) + 2
        usable_w = lw_w - (scrollbar if scrolls else 0) - 6
        cell_w = max(60, usable_w // columns)
        self.lw.setIconSize(QSize(cell_w - 4, lw_h - 4))
        self.lw.setGridSize(QSize(cell_w, lw_h))
        for i in range(count):
            it = self.lw.item(i)
            it.setSizeHint(QSize(cell_w - 4, lw_h - 4))
            w_item = self.lw.itemWidget(it)
            if w_item and hasattr(w_item, 'update_size'):
                w_item.update_size(cell_w - 4, lw_h - 4)
        self.lbl_title.setText(self.name + (f" · {count} 张" if count > 1 else ""))

    def dragEnterEvent(self, e):
        if accepts_image_drag(e.mimeData()):
            e.acceptProposedAction()
            self.setStyleSheet(self._drag_style())
            self._visual_state_key = None

    def dragLeaveEvent(self, e):
        self.set_state(self.state)
        super().dragLeaveEvent(e)

    def mousePressEvent(self, e):
        self.setFocus(Qt.MouseFocusReason)
        if e.button() == Qt.LeftButton:
            self.selected.emit(self.idx)
        super().mousePressEvent(e)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            self.setFocus(Qt.MouseFocusReason)
            self.selected.emit(self.idx)
        return super().eventFilter(obj, event)

    def keyPressEvent(self, e):
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        # Keyboard focus doubles as category selection so Tab + Ctrl+V works
        # without a mouse click, matching the pointer behaviour.
        super().focusInEvent(e)
        self.selected.emit(self.idx)

    def dropEvent(self, e):
        self.set_state(self.state)
        self.selected.emit(self.idx)
        mime = e.mimeData()
        kind, data = read_image_drop(mime)
        if kind == "files":
            self.files_dropped.emit(self.idx, data)
        elif kind == "virtual":
            self.virtual_files_dropped.emit(self.idx, data)
        elif kind == "image":
            self.image_dropped.emit(self.idx, data)
        elif kind == "bad_files":
            QMessageBox.warning(self, "提示", "请拖入 PNG、JPG、JPEG 或 BMP 图片")
        else:
            log.info("Unsupported drop formats: %s", ", ".join(mime.formats()))
            QMessageBox.information(self, "无法直接拖入", "当前拖拽来源没有提供图片数据。可以在 Telegram 里复制图片，然后点击这个分类卡片按 Ctrl+V。")
        e.acceptProposedAction()

    def trigger(self):
        self.selected.emit(self.idx)
        fs, _ = QFileDialog.getOpenFileNames(self, "选图", filter="Images (*.png *.jpg *.jpeg *.bmp)")
        if fs:
            self.files_dropped.emit(self.idx, fs)


class AutoSortDropZone(QFrame):
    """Drop target above the slot cards; the photo classifier picks slots 1-5."""

    files_dropped = Signal(list)
    image_dropped = Signal(object)
    virtual_files_dropped = Signal(list)

    IDLE_HINT = "自动放进 1-5 类；拿不准的会让你选，6-8 类请直接拖到下面的方框"
    MISSING_HINT = "缺少自动分类模型文件，请把照片拖到下面对应的方框"

    def __init__(self, available: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("AutoSortDropZone")
        self.available = bool(available)
        self.setAcceptDrops(self.available)
        # One line, so the eight slot cards below keep as much height as possible.
        self.setFixedHeight(46)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(22, 22)
        icon.setStyleSheet("background:transparent; border:none;")
        icon.setPixmap(app_icon(self, "image", Theme.PRIMARY if self.available else Theme.TEXT_MUTED).pixmap(18, 18))
        layout.addWidget(icon)

        self.lbl_title = QLabel("拖到这里自动分类" if self.available else "自动分类不可用")
        self.lbl_title.setStyleSheet(
            f"font-size:13px; font-weight:800; color:{Theme.TEXT_MAIN}; background:transparent; border:none;"
        )
        layout.addWidget(self.lbl_title)
        self.lbl_hint = ElidedLabel("")
        self.lbl_hint.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC}; background:transparent; border:none;")
        layout.addWidget(self.lbl_hint, 1)

        self.btn_choose = QPushButton("选择图片")
        style_button(self.btn_choose, icon_name="add")
        self.btn_choose.setFixedHeight(32)
        self.btn_choose.clicked.connect(self.choose_files)
        layout.addWidget(self.btn_choose)

        self.set_busy("")
        self._apply_style(dragging=False)

    def _apply_style(self, dragging: bool) -> None:
        if not self.available:
            border, bg, width = Theme.BORDER, Theme.BG_SUBTLE, 1
        elif dragging:
            border, bg, width = Theme.PRIMARY, Theme.PRIMARY_SOFT, 2
        else:
            border, bg, width = Theme.PRIMARY, Theme.BG_CARD, 1
        self.setStyleSheet(f"""
            AutoSortDropZone {{
                background-color: {bg};
                border: {width}px dashed {border};
                border-radius: 8px;
            }}
        """)

    def set_busy(self, text: str) -> None:
        self.lbl_hint.setText(text or (self.IDLE_HINT if self.available else self.MISSING_HINT))
        self.btn_choose.setEnabled(self.available and not text)

    def dragEnterEvent(self, e):
        if self.available and accepts_image_drag(e.mimeData()):
            e.acceptProposedAction()
            self._apply_style(dragging=True)

    def dragLeaveEvent(self, e):
        self._apply_style(dragging=False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._apply_style(dragging=False)
        mime = e.mimeData()
        kind, data = read_image_drop(mime)
        if kind == "files":
            self.files_dropped.emit(data)
        elif kind == "virtual":
            self.virtual_files_dropped.emit(data)
        elif kind == "image":
            self.image_dropped.emit(data)
        elif kind == "bad_files":
            QMessageBox.warning(self, "提示", "请拖入 PNG、JPG、JPEG 或 BMP 图片")
        else:
            log.info("Unsupported drop formats: %s", ", ".join(mime.formats()))
            QMessageBox.information(self, "无法直接拖入", "当前拖拽来源没有提供图片数据。可以在 Telegram 里复制图片，点击一个分类方框后按 Ctrl+V。")
        e.acceptProposedAction()

    def choose_files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "选择要自动分类的图片", filter="Images (*.png *.jpg *.jpeg *.bmp)")
        if fs:
            self.files_dropped.emit(fs)


def category_label(idx: int) -> str:
    return next((name for category_idx, name in ALL_CATEGORIES if category_idx == idx), str(idx))


class AutoSortChoiceDialog(QDialog):
    """Ask which slot an uncertain photo belongs to; chosen_category stays None when skipped."""

    def __init__(self, preview: QImage, label: str, prediction, parent=None):
        super().__init__(parent)
        self.chosen_category = None
        self.setWindowTitle("选择照片分类")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel("没把握判断这张照片是哪一类，请选一个方框")
        heading.setStyleSheet(f"font-size:15px; font-weight:800; color:{Theme.TEXT_MAIN};")
        layout.addWidget(heading)
        name = QLabel(str(label))
        name.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC};")
        layout.addWidget(name)

        picture = QLabel()
        picture.setAlignment(Qt.AlignCenter)
        picture.setMinimumHeight(200)
        if preview is not None and not preview.isNull():
            picture.setPixmap(QPixmap.fromImage(preview).scaled(440, 330, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            picture.setText("无法预览这张图片")
        layout.addWidget(picture, 1)

        probabilities = dict(prediction.ranking)
        self.candidate_buttons = {}
        candidates_row = QHBoxLayout()
        for position, category in enumerate(prediction.candidates(3)):
            button = QPushButton(f"{category_label(category)}  {probabilities.get(category, 0.0):.0%}")
            style_button(button, "Primary" if position == 0 else "Secondary")
            button.clicked.connect(lambda _checked=False, c=category: self.choose(c))
            candidates_row.addWidget(button)
            self.candidate_buttons[category] = button
        layout.addLayout(candidates_row)

        other_row = QHBoxLayout()
        other_row.addWidget(QLabel("其他方框"))
        self.combo_other = QComboBox()
        for idx, category_name in ALL_CATEGORIES:
            self.combo_other.addItem(category_name, idx)
        other_row.addWidget(self.combo_other, 1)
        put = QPushButton("放入")
        style_button(put)
        put.clicked.connect(lambda: self.choose(self.combo_other.currentData()))
        other_row.addWidget(put)
        other_row.addSpacing(12)
        skip = QPushButton("跳过")
        style_button(skip)
        skip.clicked.connect(self.reject)
        other_row.addWidget(skip)
        layout.addLayout(other_row)

    def choose(self, category) -> None:
        self.chosen_category = int(category)
        self.accept()


class OperationErrorDialog(QDialog):
    def __init__(self, title: str, summary: str, details: str = "", parent=None):
        super().__init__(parent)
        self.error_title = str(title)
        self.error_summary = str(summary)
        self.error_details = str(details).strip()
        self.setWindowTitle(self.error_title)
        self.setMinimumWidth(480)
        self.resize(560, 330 if self.error_details else 230)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        heading.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(24, 24)
        icon.setPixmap(app_icon(self, "warn", Theme.ERROR).pixmap(20, 20))
        heading.addWidget(icon, 0, Qt.AlignTop)
        title_label = QLabel(self.error_title)
        title_label.setStyleSheet(
            f"font-size:16px; font-weight:800; color:{Theme.TEXT_MAIN};"
        )
        heading.addWidget(title_label, 1)
        layout.addLayout(heading)

        summary_label = QLabel(self.error_summary)
        summary_label.setWordWrap(True)
        summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_label.setStyleSheet(f"font-size:13px; color:{Theme.TEXT_SEC};")
        layout.addWidget(summary_label)

        self.details_box = QPlainTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setPlainText(self.error_details)
        self.details_box.setPlaceholderText("没有更多技术详情")
        self.details_box.setVisible(bool(self.error_details))
        if self.error_details:
            self.details_box.setMinimumHeight(96)
            layout.addWidget(self.details_box, 1)

        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("复制详情")
        style_button(self.btn_copy, icon_name="copy")
        self.btn_copy.clicked.connect(self.copy_details)
        buttons.addWidget(self.btn_copy)
        self.btn_logs = QPushButton("打开日志目录")
        style_button(self.btn_logs, icon_name="folder")
        self.btn_logs.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))
        )
        buttons.addWidget(self.btn_logs)
        buttons.addStretch()
        close = QPushButton("关闭")
        style_button(close, "Primary")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def copy_details(self):
        parts = [self.error_title, self.error_summary]
        if self.error_details:
            parts.extend(("", "详细信息：", self.error_details))
        QApplication.clipboard().setText("\n".join(parts))
        self.btn_copy.setText("已复制")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("复制详情"))


def show_operation_error(
    parent,
    title: str,
    summary: str,
    details: str = "",
) -> int:
    return OperationErrorDialog(title, summary, details, parent).exec()


# ---------- Dialogs ----------
class RequirementsDialog(QDialog):
    reqs_changed = Signal()

    def __init__(self, root_dir, parent=None):
        super().__init__(parent)
        self.root_dir = root_dir
        self.setWindowTitle("设置验收标准")
        self.setMinimumSize(400, 500)
        self.meta_path = root_dir / META_NAME
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        title = QLabel("必须上传的照片类型")
        title.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {Theme.TEXT_MAIN};")
        layout.addWidget(title)
        
        subtitle = QLabel("未勾选的项目将不计入缺失统计")
        subtitle.setStyleSheet(f"color: {Theme.TEXT_SEC}; margin-bottom: 10px;")
        layout.addWidget(subtitle)

        d = read_meta(self.meta_path)
        loaded_reqs = d.get("__requirements__", DEFAULT_REQS)
        current_reqs = loaded_reqs if isinstance(loaded_reqs, list) else DEFAULT_REQS

        self.checks = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        set_solid_background(scroll.viewport(), Theme.BG_MAIN)
        content, self.checks = build_requirements_checklist(current_reqs)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        btn_save = QPushButton("保存设置")
        btn_save.setProperty("class", "Primary")
        style_button(btn_save, "Primary", "ok")
        btn_save.setMinimumHeight(40)
        btn_save.clicked.connect(self.save)
        layout.addWidget(btn_save)

    def save(self):
        sel = [k for k, v in self.checks.items() if v.isChecked()]
        if not sel: return QMessageBox.warning(self, "提示", "至少选一项")

        def apply_requirements(data):
            data["__requirements__"] = sel

        try:
            update_meta(self.meta_path, apply_requirements)
        except OSError as exc:
            show_operation_error(
                self,
                "验收标准保存失败",
                "原验收标准保持不变。请检查工作目录是否可写，然后重试。",
                str(exc),
            )
            return
        self.reqs_changed.emit()
        self.accept()


class BoxRequirementsDialog(QDialog):
    reqs_changed = Signal(object)

    def __init__(
        self,
        global_reqs,
        box_reqs=None,
        parent=None,
        *,
        dialog_title="设置本箱验收标准",
        heading="本箱需要的照片类型",
        subtitle="不影响全局设置，仅对当前箱号生效",
    ):
        super().__init__(parent)
        self.global_reqs = global_reqs
        self.box_reqs = box_reqs
        self.dialog_heading = heading
        self.dialog_subtitle = subtitle
        self.setWindowTitle(dialog_title)
        self.setMinimumSize(400, 520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel(self.dialog_heading)
        title.setStyleSheet(f"font-weight: bold; font-size: 16px; color: {Theme.TEXT_MAIN};")
        layout.addWidget(title)

        subtitle = QLabel(self.dialog_subtitle)
        subtitle.setStyleSheet(f"color: {Theme.TEXT_SEC}; margin-bottom: 10px;")
        layout.addWidget(subtitle)

        current_reqs = self.box_reqs if self.box_reqs is not None else self.global_reqs

        self.checks = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        set_solid_background(scroll.viewport(), Theme.BG_MAIN)
        content, self.checks = build_requirements_checklist(current_reqs)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        btns = QHBoxLayout()
        btn_reset = QPushButton("恢复全局")
        style_button(btn_reset, icon_name="refresh")
        btn_reset.clicked.connect(self.reset_to_global)
        btns.addWidget(btn_reset)

        btn_save = QPushButton("保存")
        btn_save.setProperty("class", "Primary")
        style_button(btn_save, "Primary", "ok")
        btn_save.setMinimumHeight(40)
        btn_save.clicked.connect(self.save)
        btns.addWidget(btn_save)
        layout.addLayout(btns)

    def reset_to_global(self):
        self.reqs_changed.emit(None)
        self.accept()

    def save(self):
        sel = [k for k, v in self.checks.items() if v.isChecked()]
        if not sel:
            QMessageBox.warning(self, "提示", "至少选一项")
            return
        self.reqs_changed.emit(sel)
        self.accept()
class BatchCreateDialog(QDialog):
    tasks_created = Signal()

    def __init__(self, root_dir, parent=None):
        super().__init__(parent)
        self.root_dir = root_dir
        self.created_count = 0
        self.failed_count = 0
        self.analysis = BatchAnalysis((), 0, 0, 0, 0)
        self.setWindowTitle("批量新建箱号")
        self.setMinimumSize(560, 460)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = QLabel("批量新建箱号")
        title.setStyleSheet(f"font-size:18px; font-weight:800; color:{Theme.TEXT_MAIN};")
        layout.addWidget(title)

        subtitle = QLabel("每行一个箱号；自动去空格、转大写、去重并校验")
        subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        layout.addWidget(subtitle)

        self.txt = QTextEdit()
        self.txt.setPlaceholderText("例如：MSKU1234567（可一次粘贴多行）")
        self.txt.setAcceptRichText(False)
        self.txt.textChanged.connect(self.update_preview)
        layout.addWidget(self.txt)

        self.preview = QLabel("输入后将在这里显示校验结果")
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(f"color:{Theme.TEXT_SEC}; background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; padding:10px; border-radius:8px;")
        layout.addWidget(self.preview)

        self.btn_create = QPushButton("开始创建")
        self.btn_create.setProperty("class", "Primary")
        style_button(self.btn_create, "Primary", "add")
        self.btn_create.setMinimumHeight(40)
        self.btn_create.setEnabled(False)
        self.btn_create.clicked.connect(self.run)
        layout.addWidget(self.btn_create)

    def update_preview(self):
        try:
            existing = {
                path.name for path in self.root_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            }
        except OSError:
            existing = set()
        self.analysis = analyze_batch_input(self.txt.toPlainText(), existing)
        total = len(self.analysis.creatable)
        self.btn_create.setEnabled(total > 0)
        self.btn_create.setText(f"创建 {total} 个任务" if total else "开始创建")
        preview_text = (
            f"可创建 {total} 个 · 校验通过 {self.analysis.valid_count} · "
            f"需核对 {self.analysis.warning_count} · 重复 {self.analysis.duplicate_count} · "
            f"已存在 {self.analysis.existing_count}"
        )
        hints = []
        for raw_line in re.split(r"[\r\n,，;；、\t]+", self.txt.toPlainText()):
            hint = build_container_number_hint(raw_line)
            if hint is None or hint.complete_number in hints:
                continue
            hints.append(hint.complete_number)
        if hints:
            prefix = "…" if len(hints) > 3 else ""
            preview_text += "\n校验位提示：" + prefix + "、".join(hints[-3:])
        self.preview.setText(preview_text)
        color = Theme.WARNING if self.analysis.warning_count else Theme.TEXT_SEC
        self.preview.setStyleSheet(
            f"color:{color}; background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; "
            "padding:10px; border-radius:8px;"
        )

    def run(self):
        self.created_count = 0
        self.failed_count = 0
        self.update_preview()
        if not self.analysis.creatable:
            self.preview.setText("没有可创建的箱号，请检查输入内容")
            self.preview.setStyleSheet(f"color:{Theme.ERROR}; background:{Theme.ERROR_SOFT}; border:1px solid {Theme.ERROR_LINE}; padding:10px; border-radius:8px;")
            return
        for candidate in self.analysis.creatable:
            try:
                (self.root_dir / candidate.name).mkdir(exist_ok=False)
                self.created_count += 1
            except OSError as exc:
                self.failed_count += 1
                log.warning("Create task failed for %s: %s", candidate.name, exc)
        if not self.created_count:
            self.preview.setText(f"创建失败 {self.failed_count} 个，请检查目录权限")
            self.preview.setStyleSheet(f"color:{Theme.ERROR}; background:{Theme.ERROR_SOFT}; border:1px solid {Theme.ERROR_LINE}; padding:10px; border-radius:8px;")
            return
        self.tasks_created.emit()
        self.accept()


class ExportNamingDialog(QDialog):
    def __init__(self, zip_template: str, excel_template: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置导出文件名")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)

        title = QLabel("导出文件名模板")
        title.setStyleSheet(
            f"font-size:17px; font-weight:800; color:{Theme.TEXT_MAIN};"
        )
        layout.addWidget(title)
        help_label = QLabel(
            "可用占位符：{YYYYMMDD}、{YYYYMMDD_HHMM}、{箱数}、{照片数}、{范围}。"
            "只改变导出文件名，不改变 ZIP 或 Excel 内容。"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC};")
        layout.addWidget(help_label)

        form = QFormLayout()
        self.zip_edit = QLineEdit(zip_template)
        self.excel_edit = QLineEdit(excel_template)
        form.addRow("ZIP 模板", self.zip_edit)
        form.addRow("Excel 模板", self.excel_edit)
        layout.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.preview)

        buttons = QHBoxLayout()
        reset = QPushButton("恢复默认")
        style_button(reset, icon_name="refresh")
        reset.clicked.connect(self.reset_defaults)
        buttons.addWidget(reset)
        buttons.addStretch()
        cancel = QPushButton("取消")
        style_button(cancel)
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        style_button(save, "Primary", "ok")
        save.clicked.connect(self.validate_and_accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.zip_edit.textChanged.connect(self.update_preview)
        self.excel_edit.textChanged.connect(self.update_preview)
        self.update_preview()

    def rendered_previews(self):
        context = {
            "task_count": 12,
            "photo_count": 86,
            "scope": "所选任务",
        }
        return (
            render_export_filename("zip", self.zip_edit.text(), **context),
            render_export_filename("excel", self.excel_edit.text(), **context),
        )

    def update_preview(self):
        zip_result, excel_result = self.rendered_previews()
        lines = [
            f"ZIP 预览：{zip_result.filename}",
            f"Excel 预览：{excel_result.filename}",
        ]
        errors = [
            result.error
            for result in (zip_result, excel_result)
            if result.used_fallback and result.error
        ]
        if errors:
            lines.append("需要修改：" + "；".join(errors))
        self.preview.setText("\n".join(lines))
        self.preview.setStyleSheet(
            f"background:{Theme.WARNING_SOFT if errors else Theme.BG_SUBTLE}; "
            f"border:1px solid {Theme.WARNING_LINE if errors else Theme.BORDER}; "
            "border-radius:7px; padding:10px; "
            f"color:{Theme.WARNING if errors else Theme.TEXT_SEC};"
        )

    def reset_defaults(self):
        self.zip_edit.setText(DEFAULT_EXPORT_TEMPLATES["zip"])
        self.excel_edit.setText(DEFAULT_EXPORT_TEMPLATES["excel"])

    def validate_and_accept(self):
        results = self.rendered_previews()
        invalid = [result.error for result in results if result.used_fallback]
        if invalid:
            QMessageBox.warning(
                self,
                "模板不可用",
                "；".join(error for error in invalid if error)
                or "请检查文件名模板",
            )
            return
        self.accept()

    def templates(self) -> tuple[str, str]:
        return self.zip_edit.text().strip(), self.excel_edit.text().strip()


class ThemeChoiceDialog(QDialog):
    def __init__(self, selected_theme: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择界面配色")
        self.setMinimumWidth(540)
        self._radios = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(10)

        title = QLabel("选择一套界面配色")
        title.setStyleSheet(
            f"font-size:17px; font-weight:800; color:{Theme.TEXT_MAIN};"
        )
        layout.addWidget(title)
        subtitle = QLabel(
            "只改变颜色，不改变按钮位置、数据格式或业务逻辑。保存后重启应用完整生效。"
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC};")
        layout.addWidget(subtitle)

        group = QButtonGroup(self)
        group.setExclusive(True)
        current = normalize_theme_name(selected_theme)
        for palette in THEME_PALETTES.values():
            card = QFrame()
            card.setProperty("class", "Card")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 10, 12, 10)
            row.setSpacing(12)

            radio = QRadioButton()
            radio.setChecked(palette.name == current)
            group.addButton(radio)
            self._radios[palette.name] = radio
            row.addWidget(radio)

            text_column = QVBoxLayout()
            text_column.setSpacing(2)
            name = QLabel(palette.display_name)
            name.setStyleSheet(
                f"font-size:13px; font-weight:750; color:{Theme.TEXT_MAIN};"
            )
            description = QLabel(palette.description)
            description.setStyleSheet(f"font-size:11px; color:{Theme.TEXT_MUTED};")
            text_column.addWidget(name)
            text_column.addWidget(description)
            row.addLayout(text_column, 1)

            swatches = QHBoxLayout()
            swatches.setSpacing(3)
            for key in ("BG_MAIN", "BG_CARD", "PRIMARY", "SUCCESS", "WARNING"):
                swatch = QLabel()
                swatch.setFixedSize(24, 24)
                swatch.setStyleSheet(
                    f"background:{palette.colors[key]}; "
                    f"border:1px solid {palette.colors['BORDER_HOVER']}; "
                    "border-radius:6px;"
                )
                swatches.addWidget(swatch)
            row.addLayout(swatches)
            layout.addWidget(card)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        style_button(cancel)
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存配色")
        style_button(save, "Primary", "ok")
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def selected_name(self) -> str:
        for name, radio in self._radios.items():
            if radio.isChecked():
                return name
        return DEFAULT_THEME_NAME


class ExportPreflightDialog(QDialog):
    def __init__(self, preflight: ExportPreflight, parent=None):
        super().__init__(parent)
        self.preflight = preflight
        self.setWindowTitle("导出前确认")
        self.setMinimumWidth(440)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        kind_text = "照片交接包 ZIP" if self.preflight.kind == "zip" else "采集看板 Excel"
        title = QLabel(kind_text)
        title.setStyleSheet(f"font-size:18px; font-weight:800; color:{Theme.TEXT_MAIN};")
        layout.addWidget(title)

        subtitle = QLabel("请核对本次导出范围和数量，确认后再选择保存位置。")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        layout.addWidget(subtitle)

        stats = QGridLayout()
        stats.setHorizontalSpacing(18)
        stats.setVerticalSpacing(10)
        rows = [
            ("导出范围", self.preflight.scope),
            ("任务数量", f"{self.preflight.task_count} 个"),
            (
                "任务状态",
                (
                    f"完成 {self.preflight.complete_count} · "
                    f"待补 {self.preflight.pending_count} · "
                    f"异常 {self.preflight.anomaly_count}"
                ),
            ),
            ("照片数量", f"{self.preflight.photo_count} 张"),
        ]
        if self.preflight.kind == "zip":
            rows.append(("源照片大小", format_file_size(self.preflight.source_photo_bytes)))
        self.value_labels = {}
        for row, (label_text, value_text) in enumerate(rows):
            label = QLabel(label_text)
            label.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
            value = QLabel(value_text)
            value.setWordWrap(True)
            value.setStyleSheet(f"font-size:13px; font-weight:650; color:{Theme.TEXT_MAIN};")
            stats.addWidget(label, row, 0, Qt.AlignTop)
            stats.addWidget(value, row, 1)
            self.value_labels[label_text] = value
        stats.setColumnStretch(1, 1)
        layout.addLayout(stats)

        incomplete = self.preflight.pending_count + self.preflight.anomaly_count
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setContentsMargins(10, 8, 10, 8)
        if incomplete:
            self.notice.setText(f"本次范围中有 {incomplete} 个任务尚未完全通过验收，仍会按所选范围导出。")
            self.notice.setStyleSheet(
                f"background:{Theme.WARNING_SOFT}; color:{Theme.WARNING}; "
                "border:1px solid " + Theme.WARNING_LINE + "; border-radius:7px; font-size:12px;"
            )
        else:
            self.notice.setText("所选任务均已完成验收。")
            self.notice.setStyleSheet(
                f"background:{Theme.SUCCESS_SOFT}; color:{Theme.SUCCESS}; "
                "border:1px solid " + Theme.SUCCESS_LINE + "; border-radius:7px; font-size:12px;"
            )
        layout.addWidget(self.notice)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        style_button(cancel)
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("继续并选择保存位置")
        style_button(confirm, "Primary", "export")
        confirm.setDefault(True)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)


# ---------- Image Widgets ----------
class _LoadSignals(QObject):
    loaded = Signal(object)


class ImageLoadTask(QRunnable):
    """Loads and scales an image in the thread pool, with cache support."""

    def __init__(self, path: str, size: QSize, signals: _LoadSignals, contain: bool = True):
        super().__init__()
        self.path = path
        self.size = size
        self.contain = contain
        self.signals = signals
        self._cancelled = threading.Event()
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        if self._cancelled.is_set():
            return
        cached = _thumb_cache.get(self.path, self.size)
        if cached is not None:
            if not self._cancelled.is_set():
                self.signals.loaded.emit(cached)
            return
        try:
            payload = Path(self.path).read_bytes()
        except OSError:
            if not self._cancelled.is_set():
                self.signals.loaded.emit(None)
            return
        if self._cancelled.is_set():
            return
        img = QImage.fromData(payload)
        if img.isNull():
            if not self._cancelled.is_set():
                self.signals.loaded.emit(None)
            return
        w, h = self.size.width(), self.size.height()
        if self.contain:
            img2 = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            img2 = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            if img2.width() != w or img2.height() != h:
                x = max(0, (img2.width() - w) // 2)
                y = max(0, (img2.height() - h) // 2)
                img2 = img2.copy(x, y, w, h)
        if self._cancelled.is_set():
            return
        _thumb_cache.put(self.path, self.size, img2)
        self.signals.loaded.emit(img2)

class ImageItemWidget(QWidget):
    RETRY_DELAYS_MS = (180, 650)

    def __init__(self, path, box_dir, contain=True, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.path = path
        self.box_dir = box_dir
        self.contain = contain
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.frame = QFrame()
        self.frame.setStyleSheet(f"background-color: {Theme.BG_SUBTLE}; border: 1px solid {Theme.BORDER}; border-radius: 6px;")
        fvl = QVBoxLayout(self.frame)
        fvl.setContentsMargins(2, 2, 2, 2)
        fvl.setSpacing(2)
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet(f"border: 1px solid {Theme.BORDER}; border-radius: 4px; background-color: {Theme.BG_CARD};")
        self.spinner = QLabel("加载中…")
        self.spinner.setAlignment(Qt.AlignCenter)
        self.spinner.setStyleSheet(f"color: {Theme.TEXT_SEC}; font-size: 11px;")
        fvl.addWidget(self.lbl, 0, Qt.AlignCenter)
        fvl.addWidget(self.spinner)
        v.addWidget(self.frame)
        self.pm = None
        self.aspect = None
        self._task = None
        self._signals = None
        self._disposed = False
        self._retry_count = 0
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_load)
        self.load_async()

    def load_async(self):
        if self._disposed:
            return
        p = str(self.box_dir / self.path)
        self._signals = _LoadSignals()
        self._signals.loaded.connect(self.on_loaded)
        task = ImageLoadTask(p, QSize(400, 300), self._signals, self.contain)
        self._task = task
        QThreadPool.globalInstance().start(task)

    def on_loaded(self, img):
        if self._disposed:
            return
        self._task = None
        self._signals = None
        if img is None:
            if self._retry_count < len(self.RETRY_DELAYS_MS):
                delay = self.RETRY_DELAYS_MS[self._retry_count]
                self._retry_count += 1
                self.spinner.setText("正在重试…")
                self.lbl.clear()
                self._retry_timer.start(delay)
                return
            self.spinner.setText("无法加载")
            self.lbl.setText("无法加载")
            return
        self._retry_timer.stop()
        self._retry_count = 0
        pm = QPixmap.fromImage(img)
        self.pm = pm
        self.aspect = pm.width() / max(1, pm.height())
        self.spinner.hide()
        self.update_size(self.width(), self.height())

    def _retry_load(self):
        if self._disposed:
            return
        self.spinner.setText("加载中…")
        self.load_async()

    def cancel_async_load(self):
        self._disposed = True
        self._retry_timer.stop()
        task = self._task
        if task is not None:
            task.cancel()
        signals = self._signals
        self._signals = None
        self._task = None
        if signals is not None:
            try:
                signals.loaded.disconnect(self.on_loaded)
            except (RuntimeError, TypeError):
                pass

    def update_size(self, w, h):
        if not self.pm:
            return
        avail_w = max(10, w - 8)
        avail_h = max(10, h - 8)
        if self.aspect is None:
            self.aspect = 1.333
        
        h_by_w = int(avail_w / self.aspect)
        if h_by_w <= avail_h:
            w_use, h_use = avail_w, h_by_w
        else:
            h_use = avail_h
            w_use = int(h_use * self.aspect)
            
        self.lbl.setFixedSize(w_use, h_use)
        scaled_pixmap = self.pm.scaled(self.lbl.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.lbl.setPixmap(scaled_pixmap)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_size(e.size().width(), e.size().height())

class ImageViewer(QDialog):
    """Photo viewer: fit or zoom (wheel, +/-, double-click), drag to pan, and
    ←/→ through every photo of the box in slot order."""

    MIN_ZOOM = 0.05
    MAX_ZOOM = 8.0

    def __init__(self, path, parent=None, photos=None, index=0):
        super().__init__(parent)
        self.setWindowTitle("查看图片")
        self.photos = list(photos) if photos else [(str(path), Path(str(path)).name)]
        self.index = max(0, min(int(index), len(self.photos) - 1))
        self.zoom = None  # None = fit to window
        self.pm = None
        self._drag_origin = None
        screen = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1280, 800)
        self.resize(max(900, int(screen.width() * 0.7)), max(600, int(screen.height() * 0.85)))

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("viewerBar")
        bar.setStyleSheet(f"QFrame#viewerBar {{ background:{Theme.BG_MAIN}; border-bottom:1px solid {Theme.BORDER}; }}")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        bar_layout.setSpacing(8)
        self.btn_prev = QPushButton("上一张")
        style_button(self.btn_prev, icon_name="collapse")
        self.btn_prev.setToolTip("上一张（←）")
        self.btn_prev.clicked.connect(lambda: self.show_photo(self.index - 1))
        bar_layout.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一张")
        style_button(self.btn_next, icon_name="expand")
        self.btn_next.setToolTip("下一张（→）")
        self.btn_next.clicked.connect(lambda: self.show_photo(self.index + 1))
        bar_layout.addWidget(self.btn_next)
        self.lbl_caption = ElidedLabel("")
        self.lbl_caption.setStyleSheet(f"font-size:13px; font-weight:700; color:{Theme.TEXT_MAIN};")
        bar_layout.addWidget(self.lbl_caption, 1)
        self.lbl_zoom = QLabel()
        self.lbl_zoom.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC};")
        bar_layout.addWidget(self.lbl_zoom)
        self.btn_fit = QPushButton("适应窗口")
        style_button(self.btn_fit)
        self.btn_fit.setToolTip("适应窗口（0）；滚轮或 +/- 缩放，双击切换原始大小，按住拖动查看局部")
        self.btn_fit.clicked.connect(lambda: self.set_zoom(None))
        bar_layout.addWidget(self.btn_fit)
        v.addWidget(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.lbl)
        self.scroll.viewport().installEventFilter(self)
        v.addWidget(self.scroll, 1)
        for key, step in ((Qt.Key_Left, -1), (Qt.Key_Right, 1)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda s=step: self.show_photo(self.index + s))
        for keys, factor in (((Qt.Key_Plus, Qt.Key_Equal), 1.25), ((Qt.Key_Minus,), 0.8)):
            for key in keys:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.activated.connect(lambda f=factor: self.zoom_by(f))
        QShortcut(QKeySequence(Qt.Key_0), self).activated.connect(lambda: self.set_zoom(None))
        self.show_photo(self.index)

    def show_photo(self, index: int) -> None:
        if not 0 <= index < len(self.photos):
            return
        self.index = index
        path, caption = self.photos[index]
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        self.pm = QPixmap.fromImage(image) if not image.isNull() else None
        counter = f"{index + 1} / {len(self.photos)}  ·  " if len(self.photos) > 1 else ""
        self.lbl_caption.setText(counter + str(caption))
        self.btn_prev.setEnabled(index > 0)
        self.btn_next.setEnabled(index < len(self.photos) - 1)
        self.zoom = None
        if self.pm is None:
            self.lbl.setPixmap(QPixmap())
            self.lbl.setText("无法加载图片")
            self.lbl_zoom.setText("")
            return
        self._render()

    def fit_factor(self) -> float:
        if self.pm is None or self.pm.isNull():
            return 1.0
        area = self.scroll.viewport().size()
        return min(area.width() / max(1, self.pm.width()), area.height() / max(1, self.pm.height()))

    def current_factor(self) -> float:
        return self.fit_factor() if self.zoom is None else self.zoom

    def set_zoom(self, factor) -> None:
        self.zoom = None if factor is None else max(self.MIN_ZOOM, min(self.MAX_ZOOM, float(factor)))
        self._render()

    def zoom_by(self, ratio: float) -> None:
        self.set_zoom(self.current_factor() * ratio)

    def _render(self) -> None:
        if self.pm is None:
            return
        factor = self.current_factor()
        size = QSize(max(1, int(self.pm.width() * factor)), max(1, int(self.pm.height() * factor)))
        self.lbl.setPixmap(self.pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.lbl_zoom.setText(f"{factor:.0%}" + ("（适应窗口）" if self.zoom is None else ""))

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and self.pm is not None:
            kind = event.type()
            if kind == QEvent.Wheel:
                self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8)
                return True
            if kind == QEvent.MouseButtonDblClick:
                self.set_zoom(1.0 if self.zoom is None else None)
                return True
            if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_origin = (event.position().toPoint(), self.scroll.horizontalScrollBar().value(),
                                     self.scroll.verticalScrollBar().value())
                return True
            if kind == QEvent.MouseMove and self._drag_origin is not None:
                origin, h_value, v_value = self._drag_origin
                delta = event.position().toPoint() - origin
                self.scroll.horizontalScrollBar().setValue(h_value - delta.x())
                self.scroll.verticalScrollBar().setValue(v_value - delta.y())
                return True
            if kind == QEvent.MouseButtonRelease:
                self._drag_origin = None
            if kind == QEvent.Resize and self.zoom is None:
                self._render()
        return super().eventFilter(obj, event)


def task_issue_reason(data: dict) -> str:
    if not data.get("box_number_valid", True):
        return "箱号格式或校验位异常"
    verification_status = data.get("ocr_status", "")
    if verification_status == "mismatch":
        recognized = data.get("ocr_container", "")
        return f"OCR识别为 {recognized}，与任务箱号不一致" if recognized else "OCR箱号与任务箱号不一致"
    if verification_status == "unreadable":
        return "关门照片未识别到有效箱号"
    if verification_status == "expected_invalid":
        return "任务箱号格式或校验位异常"
    if verification_status == "error":
        return "OCR识别失败，请打开任务重试"
    if (
        data.get("ocr_enabled", True)
        and data.get("has_auto_ocr_photo")
        and not verification_status
    ):
        return "关门照片待识别"
    return ""


def size_type_badge_state(size_type: str) -> str:
    """Return a quiet, semantic color state for a recognized ISO size/type."""
    value = re.sub(r"\s+", "", str(size_type or "")).upper()
    if not value:
        return "warn"
    prefix = value[:2]
    if prefix in {"20", "22"}:
        return "info"
    if prefix in {"25", "40", "42"}:
        return "ok"
    return "neutral"


class SlotStrip(QWidget):
    """One small numbered square per photo slot, so a row shows at a glance
    which photos a box still needs: filled = has photos, amber = required but
    empty, faint = optional and empty. Painted, not built from child widgets,
    because the task list can hold hundreds of rows."""

    CELL = 17
    GAP = 3
    GROUP_GAP = 8  # between the door/loading photos 1-5 and the extras 6-8

    def __init__(self, counts, required, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.counts = {int(key): int(value) for key, value in (counts or {}).items()}
        self.required = {int(category) for category in (required or [])}
        cells = len(ALL_CATEGORIES)
        self.setFixedSize(cells * self.CELL + (cells - 2) * self.GAP + self.GROUP_GAP, self.CELL)
        have = [str(idx) for idx, _ in ALL_CATEGORIES if self.counts.get(idx)]
        need = [str(idx) for idx, _ in ALL_CATEGORIES if idx in self.required and not self.counts.get(idx)]
        self.setAccessibleName(f"已有 {','.join(have) or '无'}；缺 {','.join(need) or '无'}")

    def cell_state(self, idx: int) -> str:
        if self.counts.get(idx):
            return "done"
        return "missing" if idx in self.required else "optional"

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = painter.font()
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        styles = {
            "done": (Theme.SUCCESS, Theme.SUCCESS, Theme.BG_MAIN),
            "missing": (Theme.WARNING_SOFT, Theme.WARNING, Theme.WARNING),
            "optional": (None, Theme.BORDER_HOVER, Theme.TEXT_MUTED),
        }
        x = 0
        for idx, _name in ALL_CATEGORIES:
            fill, border, text = styles[self.cell_state(idx)]
            rect = QRect(x, 0, self.CELL - 1, self.CELL - 1)
            painter.setPen(QPen(QColor(border), 1))
            painter.setBrush(QBrush(QColor(fill)) if fill else Qt.NoBrush)
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor(text))
            painter.drawText(rect, Qt.AlignCenter, str(idx))
            x += self.CELL + (self.GROUP_GAP if idx == 5 else self.GAP)
        painter.end()


class TaskRowWidget(QFrame):
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet("""
            QFrame { background: transparent; border: none; }
            QLabel { background: transparent; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        icon_box = QLabel()
        icon_box.setFixedSize(28, 28)
        icon_box.setAlignment(Qt.AlignCenter)
        icon_box.setPixmap(app_icon(self, "image", Theme.TEXT_SEC).pixmap(18, 18))
        icon_box.setStyleSheet(f"background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; border-radius:7px;")
        layout.addWidget(icon_box)

        ocr_size_type = data.get("ocr_size_type", "")
        title_text = data["name"] + (f"  ·  {ocr_size_type}" if ocr_size_type else "")
        content = QWidget(self)
        content.setAttribute(Qt.WA_TransparentForMouseEvents)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        content_layout = QGridLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setHorizontalSpacing(12)
        content_layout.setVerticalSpacing(2)
        content_layout.setColumnMinimumWidth(0, 145)
        content_layout.setColumnMinimumWidth(1, 64)
        content_layout.setColumnStretch(2, 1)

        # Keep the original combined title value for callers that use
        # fullText(), while rendering the visible title in stable columns.
        self.title_label = ElidedLabel(title_text, content)
        # It is intentionally outside the layout; keep enough width for its
        # public text() value to remain unelided for compatibility callers.
        self.title_label.setFixedWidth(512)
        QLabel.setText(self.title_label, title_text)
        self.title_label.setVisible(False)
        self.title_label.setAccessibleName("箱号和箱型")

        self.container_label = QLabel(data["name"], content)
        self.container_label.setMinimumWidth(145)
        self.container_label.setMaximumWidth(175)
        self.container_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.container_label.setToolTip(data["name"])
        self.container_label.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{Theme.TEXT_MAIN};"
        )
        content_layout.addWidget(self.container_label, 0, 0)
        self.accessible_title_label = self.title_label

        size_type_state = size_type_badge_state(ocr_size_type)
        self.size_type_label = status_badge(
            ocr_size_type or "未识别", size_type_state, compact=True
        )
        self.size_type_label.setProperty("badge_state", size_type_state)
        self.size_type_label.setFixedWidth(64)
        self.size_type_label.setToolTip(
            f"OCR 箱型：{ocr_size_type or '尚未识别'}；颜色仅区分箱型类别，不表示核对通过"
        )
        content_layout.addWidget(self.size_type_label, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)

        self.slot_strip = None
        if "category_counts" in data:
            self.slot_strip = SlotStrip(data.get("category_counts"), data.get("required_categories"), content)
            content_layout.addWidget(self.slot_strip, 0, 2, Qt.AlignLeft | Qt.AlignVCenter)

        verification_status = data.get("ocr_status", "")
        verification_badges = {
            "match": ("箱号一致", "ok"),
            "inferred_match": ("推算吻合", "warn"),
            "mismatch": ("箱号不符", "bad"),
            "unreadable": ("未识别", "warn"),
            "expected_invalid": ("任务箱号异常", "bad"),
            "error": ("OCR失败", "bad"),
        }
        self.verification_badge = None
        if verification_status in verification_badges:
            badge_text, badge_state = verification_badges[verification_status]
            self.verification_badge = status_badge(badge_text, badge_state)
            if verification_status == "inferred_match":
                resolved_number = str(
                    data.get("ocr_container") or data.get("name") or ""
                )
                self.verification_badge.setToolTip(
                    "OCR 已识别箱号前 10 位，最后一位按 ISO 6346 推算；"
                    f"最终箱号 {resolved_number} 与任务箱号一致。"
                )
        missing = data.get("missing", [])
        if missing:
            detail_text = f'{data["pic_cnt"]} 张照片 · 缺 {len(missing)} 项：{", ".join(missing[:3])}'
            if len(missing) > 3:
                detail_text += "..."
        else:
            detail_text = f'{data["pic_cnt"]} 张照片 · 必填项已完成'
        issue_reason = task_issue_reason(data)
        if issue_reason:
            detail_text += f" · {issue_reason}"
        self.detail_label = ElidedLabel(detail_text, content)
        self.detail_label.setStyleSheet(
            f"font-size:12px; color:{Theme.TEXT_MUTED};"
        )
        content_layout.addWidget(self.detail_label, 1, 0, 1, 3)
        layout.addWidget(content, 1)

        if self.verification_badge is not None:
            self.verification_badge.setFixedWidth(96)
            layout.addWidget(self.verification_badge, 0, Qt.AlignVCenter)
        else:
            layout.addSpacing(96)

        task_state = data.get("task_state", "pending")
        if (
            task_state == "anomaly"
            and data.get("box_number_valid", True)
            and data.get("ocr_enabled", True)
            and data.get("has_auto_ocr_photo")
            and verification_status in {"", "unreadable"}
        ):
            badge_text = "待核验" if not verification_status else "需核对"
            badge_style = "warn"
        else:
            task_badges = {
                "complete": ("完成", "ok"),
                "pending": ("待补", "warn"),
                "anomaly": ("异常", "bad"),
            }
            badge_text, badge_style = task_badges.get(task_state, task_badges["pending"])
        self.task_badge = status_badge(badge_text, badge_style)
        self.task_badge.setFixedWidth(64)
        layout.addWidget(self.task_badge, 0, Qt.AlignVCenter)

        arrow = QLabel()
        arrow.setFixedSize(20, 20)
        arrow.setPixmap(app_icon(self, "expand", Theme.TEXT_MUTED).pixmap(16, 16))
        arrow.setAlignment(Qt.AlignCenter)
        layout.addWidget(arrow, 0, Qt.AlignVCenter)


class ActionToast(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._callback = None
        self._secondary_callback = None
        self.setStyleSheet(f"""
            ActionToast {{ background:{Theme.INK_ELEVATED}; border:1px solid {Theme.INK_BORDER}; border-radius:8px; }}
            QLabel {{ color:{Theme.TEXT_ON_INK}; background:transparent; font-size:12px; }}
            QPushButton {{ color:{Theme.ACCENT_ON_DARK}; background:transparent; border:none; font-weight:700; padding:4px 6px; }}
            QPushButton:hover {{ color:white; }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(10)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(420)
        layout.addWidget(self.label)
        self.action = QPushButton()
        self.action.clicked.connect(self._run_action)
        layout.addWidget(self.action)
        self.secondary_action = QPushButton()
        self.secondary_action.clicked.connect(self._run_secondary_action)
        layout.addWidget(self.secondary_action)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def show_message(
        self,
        text: str,
        action_text: str = "",
        callback=None,
        duration: int = 6000,
        secondary_text: str = "",
        secondary_callback=None,
    ):
        self.label.setText(text)
        self._callback = callback
        self._secondary_callback = secondary_callback
        self.action.setText(action_text)
        self.action.setVisible(bool(action_text and callback))
        self.secondary_action.setText(secondary_text)
        self.secondary_action.setVisible(bool(secondary_text and secondary_callback))
        self.adjustSize()
        self.reposition()
        self.raise_()
        self.show()
        self.timer.start(duration)

    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.label.setMaximumWidth(max(180, min(420, parent.width() - 96)))
        self.adjustSize()
        self.move(max(16, parent.width() - self.width() - 24), max(16, parent.height() - self.height() - 26))

    def _run_action(self):
        callback = self._callback
        self.hide()
        self._callback = None
        self._secondary_callback = None
        if callback is not None:
            callback()

    def _run_secondary_action(self):
        callback = self._secondary_callback
        self.hide()
        self._callback = None
        self._secondary_callback = None
        if callback is not None:
            callback()


class OcrResultDialog(QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.original = result or {}
        self.result_data = dict(self.original)
        self.setWindowTitle("OCR 识别确认")
        self.resize(520, 640)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        level = self.original.get("confidence", {}).get("level", "low")
        inferred_check_digit = bool(self.original.get("inferred_check_digit"))
        level_text = (
            "校验位需确认"
            if inferred_check_digit
            else {"high": "高可信", "medium": "需确认", "low": "低可信"}.get(
                level,
                "需确认",
            )
        )
        self.lbl_status = status_badge(level_text, "ok" if level == "high" else "warn" if level == "medium" else "bad")
        layout.addWidget(self.lbl_status, 0, Qt.AlignLeft)

        result_heading = QLabel("解析后的最终结果（可确认或修正）")
        result_heading.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{Theme.TEXT_MAIN};"
        )
        layout.addWidget(result_heading)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.edits = {}
        fields = [
            ("container_no", "完整箱号"),
            ("size_type", "箱型代码"),
            ("max_gross_kg", "最大总重 kg"),
            ("tare_kg", "自重 kg"),
            ("payload_kg", "最大载重 kg"),
            ("capacity_m3", "容积 m³"),
        ]
        for key, label in fields:
            edit = QLineEdit("" if self.original.get(key) is None else str(self.original.get(key, "")))
            self.edits[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        self.lbl_check_digit_hint = QLabel()
        self.lbl_check_digit_hint.setWordWrap(True)
        self.lbl_check_digit_hint.setStyleSheet(
            f"color:{Theme.PRIMARY}; font-size:12px;"
        )
        layout.addWidget(self.lbl_check_digit_hint)
        self.edits["container_no"].textChanged.connect(
            self._update_container_check_digit_hint
        )
        self._update_container_check_digit_hint(
            self.edits["container_no"].text()
        )

        notes = []
        if self.original.get("check_digit_valid"):
            notes.append("箱号校验位通过")
        else:
            notes.append("箱号校验位未通过或未识别")
        if self.original.get("inferred_check_digit"):
            notes.append("校验位由前 10 位自动推算，必须人工确认")
        notes.append(f"耗时：{self.original.get('elapsed_ms', 0)} ms")
        self.lbl_notes = QLabel("；".join(notes))
        self.lbl_notes.setWordWrap(True)
        self.lbl_notes.setStyleSheet(f"color:{Theme.TEXT_SEC}; font-size:12px;")
        layout.addWidget(self.lbl_notes)

        raw = QTextEdit()
        raw.setReadOnly(True)
        raw.setMinimumHeight(180)
        raw_lines = self.original.get("raw_lines", [])
        raw.setPlainText("\n".join(f'{r.get("text", "")}    {r.get("score", 0)}' for r in raw_lines[:80]))
        layout.addWidget(QLabel("OCR 原始文本（仅供核对，不是最终箱号）"))
        layout.addWidget(raw)

        btns = QHBoxLayout()
        btns.addStretch()
        btn_cancel = QPushButton("取消")
        style_button(btn_cancel)
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("确认保存")
        style_button(btn_save, "Primary", "ok")
        btn_save.clicked.connect(self.accept_result)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        layout.addLayout(btns)

    def _update_container_check_digit_hint(self, text: str):
        hint = build_container_number_hint(text)
        if hint is None:
            self.lbl_check_digit_hint.setText(
                "输入 4 位箱主/设备代码 + 6 位序列号后，将自动提示校验位"
            )
            return
        if hint.provided_matches is None:
            message = (
                f"ISO 6346 校验位：{hint.check_digit}，"
                f"完整箱号 {hint.complete_number}"
            )
        elif hint.provided_matches:
            message = f"ISO 6346 校验通过：{hint.complete_number}"
        else:
            message = (
                f"当前校验位不符，正确完整箱号应为 {hint.complete_number}"
            )
        self.lbl_check_digit_hint.setText(message)

    def accept_result(self):
        updated = dict(self.original)
        for key, edit in self.edits.items():
            text = edit.text().strip()
            if key in {"max_gross_kg", "tare_kg", "payload_kg"}:
                updated[key] = parse_weight_value(text) if text else None
            elif key == "capacity_m3":
                try:
                    updated[key] = float(text.replace(",", ".")) if text else None
                except ValueError:
                    QMessageBox.warning(self, "格式错误", "容积必须是数字")
                    return
            else:
                updated[key] = text.upper()
        updated["manual_corrected"] = any(
            str(updated.get(k, "")) != str(self.original.get(k, ""))
            for k in self.edits
        )
        updated["container_no"] = normalize_container_candidate(updated.get("container_no", ""))
        updated["check_digit_valid"] = validate_container_no(updated["container_no"])
        if str(updated.get("container_no", "")) != str(self.original.get("container_no", "")):
            updated["inferred_check_digit"] = False
        updated["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
        self.result_data = updated
        self.accept()

# ---------- Detail Window ----------
class BoxDetailWindow(QWidget):
    data_changed = Signal()
    back_requested = Signal()
    navigate_requested = Signal(int)

    def __init__(self, root_dir, box_name, parent=None):
        super().__init__(parent)
        set_solid_background(self, Theme.BG_MAIN)
        self.root_dir = root_dir
        self.box_name = box_name
        self.box_dir = root_dir / box_name
        self.meta_path = root_dir / META_NAME
        self.reqs = DEFAULT_REQS
        self.box_reqs = None
        self.ocr_result = {}
        self._ocr_task = None
        self._ocr_automatic = False
        self._pending_auto_ocr = None
        self.selected_category = None
        self._image_hash_index = None
        self._image_import_task = None
        self._image_import_queue = []
        self._auto_sort_task = None
        self._auto_sort_queue = []
        self._pending_auto_sort_report = None
        self._page_disposed = False

        self._photo_retry_count = 0
        self._photo_retry_timer = QTimer(self)
        self._photo_retry_timer.setSingleShot(True)
        self._photo_retry_timer.timeout.connect(self._retry_photo_reconciliation)

        d = read_meta(self.meta_path)
        loaded_reqs = d.get("__requirements__", DEFAULT_REQS)
        self.reqs = loaded_reqs if isinstance(loaded_reqs, list) else DEFAULT_REQS

        self.setWindowTitle(f"详情 - {box_name}")
        self.box_dir.mkdir(exist_ok=True)
        self.meta = {}
        self.load_meta()
        self.min_w = 190
        self.max_w = 560
        self.min_card_h = 210
        self.init_ui()

    def load_meta(self):
        all_data = read_meta(self.meta_path)

        # Keep the global requirement set fresh so reload_from_disk() picks up
        # changes made in the global requirements dialog while this page is open.
        loaded_reqs = all_data.get("__requirements__", DEFAULT_REQS)
        self.reqs = loaded_reqs if isinstance(loaded_reqs, list) else DEFAULT_REQS

        my_data = all_data.get(self.box_name, {})
        if not isinstance(my_data, dict):
            my_data = {}
        clean = {str(index): [] for index in range(1, 9)}
        dirty = False
        photo_state = resolve_task_photos(
            self.box_dir,
            my_data,
            self.reqs,
            category_count=8,
        )
        for photo in photo_state.photos:
            if Path(photo.filename).suffix.lower() in IMAGE_EXTENSIONS:
                clean[str(photo.category)].append(photo.filename)

        for i in range(1, 9):
            k = str(i)
            raw_files = my_data.get(k, [])
            if not isinstance(raw_files, list):
                raw_files = []
                dirty = True
            if clean[k] != raw_files:
                dirty = True
        self.meta = clean
        box_reqs = my_data.get("__requirements__", None)
        self.box_reqs = box_reqs if isinstance(box_reqs, list) else None
        ocr_result = my_data.get("__ocr__", {})
        self.ocr_result = current_ocr_result(ocr_result, self.box_name, self.box_dir)
        if dirty and any(
            name not in (my_data.get(key) if isinstance(my_data.get(key), list) else [])
            for key, names in clean.items() for name in names
        ):
            # A failed stat on a network share is not proof of deletion. Only
            # add recovered files here; explicit user actions handle removals.
            def recover(all_data):
                box = all_data.setdefault(self.box_name, {})
                for key, names in clean.items():
                    current = box.get(key, [])
                    current = list(current) if isinstance(current, list) else []
                    for name in names:
                        if name not in current:
                            current.append(name)
                    box[key] = current
            try:
                update_meta(self.meta_path, recover)
            except OSError:
                log.exception('Unable to persist recovered photo references')
        self._saved_box = json.loads(json.dumps(self._box_metadata()))
        unresolved = any(
            isinstance(names, list) and any(
                isinstance(name, str) and Path(name).name == name and name not in clean.get(key, [])
                for name in names
            ) for key, names in my_data.items() if key in clean
        )
        if unresolved and self._photo_retry_count < 3:
            self._photo_retry_count += 1
            self._photo_retry_timer.start(1000 * self._photo_retry_count)
        elif not unresolved:
            self._photo_retry_count = 0
            self._photo_retry_timer.stop()

    def _retry_photo_reconciliation(self):
        if not self._page_disposed:
            self.reload_from_disk(categories=set())

    def _box_metadata(self):
        box = dict(self.meta)
        if self.box_reqs is not None:
            box['__requirements__'] = self.box_reqs
        if self.ocr_result:
            box['__ocr__'] = self.ocr_result
        return box

    def save_meta(self, show_error=True):
        box = self._box_metadata()
        previous = getattr(self, '_saved_box', {})

        def apply_box_update(all_data):
            current = all_data.get(self.box_name, {})
            current = dict(current) if isinstance(current, dict) else {}
            for key in set(box) | set(previous):
                if box.get(key) == previous.get(key):
                    continue
                if key.isdigit():
                    before = previous.get(key, [])
                    after = box.get(key, [])
                    removed = set(before) - set(after)
                    names = current.get(key, [])
                    names = list(names) if isinstance(names, list) else []
                    names = [name for name in names if name not in removed]
                    for name in after:
                        if name not in before and name not in names:
                            names.append(name)
                    current[key] = names
                elif key in box:
                    current[key] = box[key]
                else:
                    current.pop(key, None)
            all_data[self.box_name] = current
            if "__requirements__" not in all_data:
                all_data["__requirements__"] = self.reqs

        try:
            update_meta(self.meta_path, apply_box_update)
        except OSError as exc:
            log.exception("Failed to save task metadata for %s", self.box_name)
            if show_error:
                show_operation_error(
                    self,
                    "任务数据保存失败",
                    "本次修改没有写入元数据；新增照片会自动回退。请检查工作目录是否可写。",
                    str(exc),
                )
            return False
        self._saved_box = json.loads(json.dumps(box))
        self.data_changed.emit()
        return True

    def _get_image_hash_index(self):
        if self._image_hash_index is None:
            self._image_hash_index = build_image_hash_index(self.box_dir, self.meta)
        return self._image_hash_index

    def _forget_image_filename(self, filename: str):
        if self._image_hash_index is None:
            return
        for digest, indexed_name in list(self._image_hash_index.items()):
            if indexed_name == filename:
                self._image_hash_index.pop(digest, None)

    def _find_image_category(self, filename: str):
        for idx, _name in ALL_CATEGORIES:
            if filename in self.meta.get(str(idx), []):
                return idx
        return None

    @staticmethod
    def _category_label(idx: int) -> str:
        return next((name for category_idx, name in ALL_CATEGORIES if category_idx == idx), str(idx))

    def _notify_duplicate_image(self, target_idx: int, filename: str):
        source_idx = self._find_image_category(filename)
        if source_idx is None:
            self._notify("已跳过重复图片")
            return
        if source_idx == target_idx:
            self._notify(f"“{self._category_label(target_idx)}”中已经有这张照片")
            return
        self._notify(
            f"这张照片已在“{self._category_label(source_idx)}”，未重复保存",
            "移到这里",
            lambda: self._move_image_to_category(filename, source_idx, target_idx),
        )

    def _move_image_to_category(self, filename: str, source_idx: int, target_idx: int,
                                restore_name: str = "", undo_state=None):
        """Move one photo to another slot and rename it to that slot's next free name.

        Only PhotoCollect names (<box>-<N>[_k].ext) are renamed, because disk
        recovery and the training tool read the category from them. The file is
        renamed first and renamed back if meta cannot be saved. `restore_name`
        and `undo_state` are set when this call undoes an earlier move.
        """
        undoing = undo_state is not None
        verb = "撤销" if undoing else "移动"

        def try_later(text):
            if undoing:
                self._notify(text, "撤销", lambda: self._move_image_to_category(
                    filename, source_idx, target_idx, restore_name, undo_state))
            else:
                self._notify(text)

        if self._page_disposed or source_idx == target_idx:
            return False
        source_key, target_key = str(source_idx), str(target_idx)
        if filename not in self.meta.get(source_key, []):
            self.reload_from_disk()
            self._notify(f"照片位置已经变化，没有{verb}")
            return False
        if self._image_import_task is not None or self._image_import_queue:
            try_later(f"照片还在保存，请稍后再{verb}")
            return False
        ocr_task = self._ocr_task
        if ocr_task is not None and Path(getattr(ocr_task, "image_path", "")).name == filename:
            try_later(f"这张照片正在识别，请稍后再{verb}")
            return False

        old_path = self.box_dir / filename
        name_category = _infer_app_named_category(self.box_name, filename, len(ALL_CATEGORIES))
        try:
            before = old_path.stat()
            new_name = filename
            if name_category is not None and name_category != target_idx:
                new_name = self._rename_into_category(old_path, target_idx, restore_name)
        except OSError as exc:
            log.warning("Moving photo %s failed: %s", filename, exc)
            self.reload_from_disk()
            show_operation_error(
                self,
                "照片移动失败",
                "照片和分类记录保持原状。照片可能正被其他程序打开，关闭后再试。",
                str(exc),
            )
            return False
        new_path = self.box_dir / new_name
        try:
            after = new_path.stat()
        except OSError:
            after = None

        meta_before = {key: list(self.meta.get(key, [])) for key in (source_key, target_key)}
        ocr_before = json.loads(json.dumps(self.ocr_result))
        self.meta[source_key] = [name for name in meta_before[source_key] if name != filename]
        self.meta[target_key] = list(meta_before[target_key])
        if new_name not in self.meta[target_key]:
            self.meta[target_key].append(new_name)
        restored = (undoing and new_name == restore_name
                    and self.ocr_result == undo_state["ocr_after"])
        recheck = None
        if restored:
            # Nothing has touched OCR since the move, so its evidence is valid again.
            self.ocr_result = undo_state["ocr_before"]
        else:
            recheck = self._update_ocr_for_move(filename, new_name, source_idx, target_idx, before, after)
        if not self.save_meta(show_error=False):
            self.meta.update(meta_before)
            self.ocr_result = ocr_before
            try:
                if new_name != filename:
                    os.rename(new_path, old_path)
            except OSError as exc:
                log.exception("Unable to restore the name of %s", filename)
                self.reload_from_disk()
                show_operation_error(
                    self,
                    "照片移动失败",
                    f"分类记录没有保存，照片也没能改回原名，现在叫 {new_name}。"
                    f"重新打开任务后，它会按文件名显示在“{self._category_label(target_idx)}”。"
                    "请检查工作目录是否可写。",
                    str(exc),
                )
                return False
            show_operation_error(
                self,
                "照片移动失败",
                "分类记录没有保存，照片保持原名留在原分类。请检查工作目录是否可写。",
                str(self.meta_path),
            )
            return False

        if self._image_hash_index is not None:
            for digest, indexed_name in list(self._image_hash_index.items()):
                if indexed_name == filename:
                    self._image_hash_index[digest] = new_name
        self.refresh_card(source_idx)
        self.refresh_card(target_idx)
        self.select_category(target_idx)
        self.refresh_header_state()
        if recheck is not None:
            self.maybe_auto_ocr(recheck[0], [recheck[1]], force=True)
        if not restored and target_idx in AUTO_OCR_CATEGORIES:
            self.maybe_auto_ocr(target_idx, [new_name])
        label = self._category_label(target_idx)
        if undoing:
            self._notify(f"已撤销，照片回到“{label}”")
            return True
        ocr_after = json.loads(json.dumps(self.ocr_result))
        renamed = f"，改名为 {new_name}" if new_name != filename else ""
        self._notify(
            f"照片已移到“{label}”{renamed}",
            "撤销",
            lambda: self._move_image_to_category(
                new_name, target_idx, source_idx, filename,
                {"ocr_before": ocr_before, "ocr_after": ocr_after},
            ),
        )
        return True

    def _rename_into_category(self, path: Path, idx: int, preferred_name: str = "") -> str:
        """Rename a photo to slot `idx`'s next free name, or to `preferred_name` while it is free."""
        for _attempt in range(20):
            if preferred_name and not (self.box_dir / preferred_name).exists():
                name, destination = preferred_name, self.box_dir / preferred_name
            else:
                name, destination = self._next_image_path(idx, path.suffix.lower())
            try:
                # On Windows os.rename never replaces an existing file, so a
                # name taken a moment ago just moves on to the next one.
                os.rename(path, destination)
                return name
            except FileExistsError:
                preferred_name = ""
        raise FileExistsError(f"No free photo name in category {idx}")

    def _update_ocr_for_move(self, old_name, new_name, source_idx, target_idx, before, after):
        """Keep OCR evidence tied to door photos (slots 4/5) after a move.

        Returns (category, name) of a door photo to recognize again, or None.
        """
        result = self.ocr_result or {}
        listed = result.get('source_images')
        sources = set(listed if isinstance(listed, list) else [])
        sources.update((result.get('source_image'), result.get('pending_source')))
        was_door = source_idx in AUTO_OCR_CATEGORIES
        is_door = target_idx in AUTO_OCR_CATEGORIES
        if old_name in sources and was_door and not is_door:
            # The result came from a photo that is no longer a door photo.
            for idx in (5, 4):
                names = self.meta.get(str(idx), [])
                if names:
                    self.ocr_result = {'pending_source': names[-1], 'verification_status': 'unreadable'}
                    return idx, names[-1]
            self.ocr_result = {}
        elif old_name in sources and new_name != old_name:
            self.ocr_result = self._renamed_ocr_result(result, old_name, new_name, before, after)
        elif is_door and not was_door:
            # Same as adding a door photo: it becomes the evidence to check.
            self.ocr_result = {'pending_source': new_name, 'verification_status': 'unreadable'}
        return None

    @staticmethod
    def _renamed_ocr_result(result, old_name, new_name, before, after):
        renamed = json.loads(json.dumps(result))
        for key in ('source_image', 'pending_source'):
            if renamed.get(key) == old_name:
                renamed[key] = new_name
        if isinstance(renamed.get('source_images'), list):
            renamed['source_images'] = [new_name if name == old_name else name for name in renamed['source_images']]
        for key in ('source_sha256', 'source_signatures'):
            values = renamed.get(key)
            if isinstance(values, dict) and old_name in values:
                values[new_name] = values.pop(old_name)
        signatures = renamed.get('source_signatures')
        signature = signatures.get(new_name) if isinstance(signatures, dict) else None
        if after is not None and signature and list(signature) == [before.st_size, before.st_mtime_ns, before.st_ctime_ns]:
            # A rename keeps the content; carry a still-valid signature over even
            # where the file system updates a timestamp on rename.
            signatures[new_name] = [after.st_size, after.st_mtime_ns, after.st_ctime_ns]
        return renamed

    def _rollback_added_files(self, idx, filenames):
        files = self.meta.setdefault(str(idx), [])
        for filename in filenames:
            while filename in files:
                files.remove(filename)
            self._forget_image_filename(filename)
            try:
                (self.box_dir / filename).unlink(missing_ok=True)
            except OSError:
                log.exception("Failed to roll back image %s", filename)

    def _commit_added_images(self, idx, filenames):
        if not filenames:
            return False
        previous_ocr = self.ocr_result
        if idx in AUTO_OCR_CATEGORIES:
            self.ocr_result = {'pending_source': filenames[-1], 'verification_status': 'unreadable'}
        if not self.save_meta():
            self.ocr_result = previous_ocr
            self._rollback_added_files(idx, filenames)
            return False
        self.refresh_card(idx)
        self.refresh_header_state()
        self.maybe_auto_ocr(idx, filenames)
        if len(filenames) == 1:
            self.focus_next_missing(idx)
        return True

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Detail header
        header = QFrame()
        header.setStyleSheet(f"background-color: {Theme.BG_MAIN}; border-bottom: 1px solid {Theme.BORDER};")
        header.setFixedHeight(82)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 7)
        header_layout.setSpacing(5)

        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)

        self.btn_back = QPushButton()
        self.btn_back.setIcon(app_icon(self.btn_back, "back", Theme.TEXT_MAIN))
        self.btn_back.setIconSize(QSize(17, 17))
        self.btn_back.setFixedSize(34, 34)
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.setToolTip("返回列表")
        self.btn_back.setStyleSheet(f"""
            QPushButton {{
                border: 1px solid {Theme.BORDER};
                border-radius: 7px;
                background: {Theme.BG_CARD};
                color: {Theme.TEXT_MAIN};
                padding: 0px;
            }}
            QPushButton:hover {{
                background: {Theme.BG_ACTIVE};
                border-color: {Theme.BORDER_HOVER};
            }}
        """)
        self.btn_back.clicked.connect(self.back_requested.emit)
        primary_row.addWidget(self.btn_back)

        self.lbl_name = QLabel()
        self.lbl_name.setToolTip(str(self.box_dir))
        self.lbl_name.setStyleSheet(f"font-size:17px; font-weight:800; color:{Theme.TEXT_MAIN};")
        primary_row.addWidget(self.lbl_name)
        self.lbl_position = QLabel()
        self.lbl_position.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        self.lbl_position.setToolTip("在当前任务列表（含筛选和排序）中的位置")
        primary_row.addWidget(self.lbl_position)
        primary_row.addStretch()

        self.btn_prev_box = QPushButton("上一箱")
        style_button(self.btn_prev_box, icon_name="collapse")
        self.btn_prev_box.setFixedHeight(34)
        self.btn_prev_box.setToolTip("打开列表中的上一个箱子（PgUp）")
        self.btn_prev_box.clicked.connect(lambda: self.navigate_requested.emit(-1))
        primary_row.addWidget(self.btn_prev_box)
        self.btn_next_box = QPushButton("下一箱")
        style_button(self.btn_next_box, icon_name="expand")
        self.btn_next_box.setFixedHeight(34)
        self.btn_next_box.setToolTip("打开列表中的下一个箱子（PgDn）")
        self.btn_next_box.clicked.connect(lambda: self.navigate_requested.emit(1))
        primary_row.addWidget(self.btn_next_box)
        self.set_navigation(0, 0)

        self.btn_next_missing = QPushButton("下一缺失项")
        style_button(self.btn_next_missing, icon_name="expand")
        self.btn_next_missing.setFixedHeight(34)
        self.btn_next_missing.setToolTip("定位到下一个尚未上传的必填分类")
        self.btn_next_missing.clicked.connect(self.focus_next_missing)
        primary_row.addWidget(self.btn_next_missing)

        self.btn_ocr = QToolButton()
        self.btn_ocr.setIcon(app_icon(self.btn_ocr, "view", Theme.TEXT_SEC))
        self.btn_ocr.setFixedSize(34, 34)
        self.btn_ocr.setToolTip("重新识别箱号和箱型")
        style_command_button(self.btn_ocr)
        self.btn_ocr.clicked.connect(self.run_ocr_from_first_image)
        primary_row.addWidget(self.btn_ocr)

        self.btn_more = QToolButton()
        self.btn_more.setIcon(app_icon(self.btn_more, "more", Theme.TEXT_SEC))
        self.btn_more.setFixedSize(34, 34)
        self.btn_more.setToolTip("更多")
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        style_command_button(self.btn_more)
        more_menu = styled_menu(self.btn_more)
        req_action = more_menu.addAction("本箱验收标准")
        req_action.setIcon(app_icon(self, "settings"))
        req_action.triggered.connect(self.open_box_reqs)
        self.btn_more.setMenu(more_menu)
        primary_row.addWidget(self.btn_more)
        header_layout.addLayout(primary_row)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(42, 0, 0, 0)
        status_row.setSpacing(7)
        self.lbl_stat = QLabel()
        status_row.addWidget(self.lbl_stat)
        self.lbl_ocr_status = QLabel()
        status_row.addWidget(self.lbl_ocr_status)
        status_row.addStretch()
        header_layout.addLayout(status_row)
        layout.addWidget(header)

        self.ocr_banner = QFrame()
        self.ocr_banner.setStyleSheet(f"background:{Theme.ERROR_SOFT}; border-bottom:1px solid {Theme.ERROR_LINE};")
        banner_layout = QHBoxLayout(self.ocr_banner)
        banner_layout.setContentsMargins(18, 7, 18, 7)
        self.lbl_ocr_banner = QLabel()
        self.lbl_ocr_banner.setWordWrap(True)
        self.lbl_ocr_banner.setStyleSheet(f"color:{Theme.ERROR}; font-size:12px; font-weight:700;")
        banner_layout.addWidget(self.lbl_ocr_banner, 1)
        retry = QPushButton("重新识别")
        style_button(retry, icon_name="refresh")
        retry.clicked.connect(self.run_ocr_from_first_image)
        banner_layout.addWidget(retry)
        self.ocr_banner.hide()
        layout.addWidget(self.ocr_banner)

        auto_sort_row = QWidget()
        set_solid_background(auto_sort_row, Theme.BG_MAIN)
        self._auto_sort_row_layout = QHBoxLayout(auto_sort_row)
        self._auto_sort_row_layout.setContentsMargins(16, 12, 16, 0)
        self.auto_sort_zone = AutoSortDropZone(get_photo_classifier() is not None)
        self.auto_sort_zone.files_dropped.connect(self.auto_sort_files)
        self.auto_sort_zone.image_dropped.connect(self.auto_sort_image)
        self.auto_sort_zone.virtual_files_dropped.connect(self.auto_sort_virtual_files)
        self._auto_sort_row_layout.addWidget(self.auto_sort_zone)
        layout.addWidget(auto_sort_row)

        # Use FlowLayout instead of QGridLayout
        self.main_widget = SolidColorViewport(Theme.BG_MAIN)
        self.main_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_widget.setStyleSheet(f"background:{Theme.BG_MAIN};")
        set_solid_background(self.main_widget, Theme.BG_MAIN)
        self.flow_layout = FlowLayout(self.main_widget, margin=16, h_spacing=12, v_spacing=12)

        self.cards = {}
        for idx, name in ALL_CATEGORIES:
            is_req = (idx in self.get_effective_reqs())
            card = DropZoneCard(idx, name, is_req)
            card.files_dropped.connect(self.add_files)
            card.image_dropped.connect(self.add_image_data)
            card.virtual_files_dropped.connect(self.add_virtual_files)
            card.selected.connect(self.select_category)

            card.lw.setContextMenuPolicy(Qt.CustomContextMenu)
            card.lw.customContextMenuRequested.connect(lambda p, w=card.lw, i=idx: self.ctx(p, w, i))
            card.lw.itemDoubleClicked.connect(lambda it, w=card.lw, i=idx: self.preview(it, w, i))
            card.lw.itemClicked.connect(lambda _item, i=idx: self.select_category(i))

            self.cards[idx] = card
            self.flow_layout.addWidget(card)
            self.refresh_card(idx)

        # Scroll Area
        scroll = QScrollArea()
        scroll.setViewport(SolidColorViewport(Theme.BG_MAIN, scroll))
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setBackgroundRole(QPalette.Window)
        scroll.setStyleSheet(
            f"QScrollArea {{ border:none; background:{Theme.BG_MAIN}; }} "
            f"QWidget#detailViewport {{ background:{Theme.BG_MAIN}; }}"
        )
        set_solid_background(scroll, Theme.BG_MAIN)
        scroll.setWidget(self.main_widget)
        scroll.viewport().setObjectName("detailViewport")
        # The banner or a window resize changes the viewport height, which the
        # fit-to-screen card height depends on.
        scroll.viewport().installEventFilter(self)
        set_solid_background(scroll.viewport(), Theme.BG_MAIN)
        layout.addWidget(scroll)
        self.scroll_area = scroll

        self.paste_shortcut = QShortcut(QKeySequence.Paste, self)
        self.paste_shortcut.setContext(Qt.WindowShortcut)
        self.paste_shortcut.activated.connect(self.paste_to_selected_category)
        self.back_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.back_shortcut.setContext(Qt.WindowShortcut)
        self.back_shortcut.activated.connect(self.back_requested.emit)
        self.prev_box_shortcut = QShortcut(QKeySequence(Qt.Key_PageUp), self)
        self.prev_box_shortcut.setContext(Qt.WindowShortcut)
        self.prev_box_shortcut.activated.connect(lambda: self.navigate_requested.emit(-1))
        self.next_box_shortcut = QShortcut(QKeySequence(Qt.Key_PageDown), self)
        self.next_box_shortcut.setContext(Qt.WindowShortcut)
        self.next_box_shortcut.activated.connect(lambda: self.navigate_requested.emit(1))
        effective = self.get_effective_reqs()
        initial = next((idx for idx in effective if not self.meta.get(str(idx), [])), effective[0] if effective else 1)
        self.select_category(initial)
        self.setTabOrder(self.btn_back, self.btn_prev_box)
        self.setTabOrder(self.btn_prev_box, self.btn_next_box)
        self.setTabOrder(self.btn_next_box, self.btn_next_missing)
        self.setTabOrder(self.btn_next_missing, self.btn_ocr)
        self.setTabOrder(self.btn_ocr, self.btn_more)
        self.setTabOrder(self.btn_more, self.auto_sort_zone.btn_choose)
        previous = self.auto_sort_zone.btn_choose
        for idx in sorted(self.cards):
            card = self.cards[idx]
            self.setTabOrder(previous, card)
            self.setTabOrder(card, card.btn_add)
            self.setTabOrder(card.btn_add, card.lw)
            previous = card.lw

        self.apply_responsive_layout()
        QTimer.singleShot(0, self.apply_responsive_layout)

        self.refresh_header_state()

    def get_effective_reqs(self):
        return self.box_reqs if self.box_reqs is not None else self.reqs

    def set_navigation(self, position: int, total: int) -> None:
        """Show "position / total" and enable the previous/next box buttons."""
        self.lbl_position.setText(f"{position} / {total}" if position and total else "")
        self.btn_prev_box.setEnabled(position > 1)
        self.btn_next_box.setEnabled(0 < position < total)

    def has_pending_saves(self) -> bool:
        return bool(
            self._image_import_task is not None
            or self._image_import_queue
            or self._auto_sort_task is not None
            or self._auto_sort_queue
        )

    def update_box_title(self):
        size_type = self.ocr_result.get("size_type", "") if self.ocr_result else ""
        self.lbl_name.setText(self.box_name + (f"  ·  {size_type}" if size_type else ""))

    def refresh_header_state(self):
        self.ocr_result = current_ocr_result(self.ocr_result, self.box_name, self.box_dir)
        self.update_stat()
        self.update_ocr_status_label()
        self.update_box_title()
        self.update_ocr_banner()

    def update_ocr_banner(self):
        status = self.ocr_result.get("verification_status", "") if self.ocr_result else ""
        message = ""
        if status == "mismatch":
            message = (
                f"箱号不一致：任务 {self.ocr_result.get('declared_container_no') or self.box_name}，"
                f"OCR识别 {self.ocr_result.get('container_no') or '未识别'}"
            )
        elif status == "unreadable":
            message = self.ocr_result.get('verification_reason') or "关门照片未识别到有效箱号，请检查图片清晰度或重新识别"
        elif status == "expected_invalid":
            message = "当前任务箱号格式或校验位异常，请核对任务名称"
        elif status == "error":
            message = f"OCR识别失败：{self.ocr_result.get('error_message', '组件或图片处理异常')}"
        self.lbl_ocr_banner.setText(message)
        self.ocr_banner.setVisible(bool(message))

    def select_category(self, idx: int):
        if idx not in self.cards:
            return
        self.selected_category = idx
        for card_idx, card in self.cards.items():
            card.set_selected(card_idx == idx)

    def paste_to_selected_category(self):
        idx = self.selected_category
        if idx not in self.cards:
            self._notify("请先选择照片分类")
            return
        image = QApplication.clipboard().image()
        if image.isNull():
            self._notify("剪贴板中没有图片")
            return
        self.add_image_data(idx, image)

    def focus_next_missing(self, start_after=None):
        if isinstance(start_after, bool):
            start_after = None
        required = list(self.get_effective_reqs())
        if start_after in required:
            position = required.index(start_after) + 1
            required = required[position:] + required[:position]
        missing = [idx for idx in required if not self.meta.get(str(idx), [])]
        if not missing:
            self._notify("必填照片已经补齐")
            return
        idx = missing[0]
        self.select_category(idx)
        self.scroll_area.ensureWidgetVisible(self.cards[idx], 24, 24)

    def reload_from_disk(self, invalidate_hashes=True, categories=None):
        if invalidate_hashes:
            self._image_hash_index = None
        before = {key: list(names) for key, names in self.meta.items()}
        self.load_meta()
        effective = self.get_effective_reqs()
        for idx, card in self.cards.items():
            card.set_required(idx in effective, refresh_width=False)
        refresh_categories = (
            tuple(self.cards)
            if categories is None
            else tuple(idx for idx in self.cards if idx in categories or before.get(str(idx)) != self.meta.get(str(idx)))
        )
        for idx in refresh_categories:
            self.refresh_card(idx)
        self.refresh_header_state()

    def _notify(self, text: str, action_text: str = "", callback=None):
        host = self.window()
        if hasattr(host, "show_toast"):
            host.show_toast(text, action_text, callback)
        else:
            log.info("%s", text)

    def apply_responsive_layout(self):
        try:
            vw = self.scroll_area.viewport().width()
        except RuntimeError:
            return
        dyn_spacing = max(8, min(14, vw // 90))
        self.flow_layout._h_spacing = dyn_spacing
        self.flow_layout._v_spacing = dyn_spacing
        dyn_margin = max(10, min(16, vw // 70))
        self.flow_layout.setContentsMargins(dyn_margin, dyn_margin, dyn_margin, dyn_margin)
        self._auto_sort_row_layout.setContentsMargins(dyn_margin, 12, dyn_margin, 0)
        margin_total = dyn_margin * 2
        spacing = dyn_spacing

        def column_width(available):
            for cols in range(4, 0, -1):
                # One pixel spare: FlowLayout wraps a row that ends exactly on the edge.
                w = (available - margin_total - spacing * (cols - 1) - 1) // cols
                if w >= self.min_w:
                    return cols, w
            return 1, max(self.min_w, available - margin_total)

        # Fit every card on screen (e.g. 4 x 2 at 1366x768) when the cards
        # stay usable; otherwise let them keep a 4:3 photo area and scroll.
        cols, best_w = column_width(vw)
        rows = -(-len(self.cards) // cols)
        vh = self.scroll_area.viewport().height()
        fit_h = (vh - margin_total - spacing * (rows - 1)) // rows
        card_h = fit_h if fit_h >= self.min_card_h else None
        if card_h is None and not self.scroll_area.verticalScrollBar().isVisible():
            # The scroll bar is about to take some width; size for it now so
            # the last card in a row does not wrap to an extra row.
            cols, best_w = column_width(vw - self.style().pixelMetric(QStyle.PM_ScrollBarExtent))
        best_w = min(best_w, self.max_w)

        for c in self.cards.values():
            c.set_card_size(best_w, card_h)
        self.flow_layout.invalidate()
        self.flow_layout.setGeometry(self.main_widget.rect())
        self.main_widget.updateGeometry()

        compact_header = self.width() < 760
        for button, text in ((self.btn_prev_box, "上一箱"), (self.btn_next_box, "下一箱")):
            button.setText("" if compact_header else text)
            if compact_header:
                button.setFixedWidth(34)
            else:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)
            button.setVisible(self.width() >= 560)
        self.btn_next_missing.setText("" if compact_header else "下一缺失项")
        if compact_header:
            self.btn_next_missing.setFixedWidth(34)
        else:
            self.btn_next_missing.setMinimumWidth(0)
            self.btn_next_missing.setMaximumWidth(16777215)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.apply_responsive_layout()

    def eventFilter(self, obj, event):
        scroll_area = getattr(self, "scroll_area", None)
        if (
            event.type() == QEvent.Resize
            and scroll_area is not None
            and obj is scroll_area.viewport()
            and not self._page_disposed
        ):
            self.apply_responsive_layout()
        return super().eventFilter(obj, event)

    def _use_background_image_import(self) -> bool:
        # Offscreen tests keep their historical synchronous public contract;
        # production Windows runs every import batch through ImageImportTask.
        return QApplication.platformName().lower() != "offscreen"

    def _queue_image_import(self, idx: int, sources: list) -> None:
        if not sources:
            return
        self._image_import_queue.append((int(idx), list(sources)))
        if self._image_import_task is None:
            self._start_next_image_import()
        else:
            queued = sum(len(batch) for _category, batch in self._image_import_queue)
            self._notify(f"照片已加入后台保存队列（待处理 {queued} 张）")

    def _start_next_image_import(self) -> None:
        if self._page_disposed or self._image_import_task is not None:
            return
        if not self._image_import_queue:
            return
        idx, sources = self._image_import_queue.pop(0)
        task = ImageImportTask(self.root_dir, self.box_name, idx, sources)
        self._image_import_task = task
        task.signals.progress.connect(
            lambda current, total, label: self._on_image_import_progress(
                idx,
                current,
                total,
                label,
            )
        )
        task.signals.finished.connect(
            lambda summary: self._on_image_import_finished(idx, task, summary)
        )
        host = self.window()
        if hasattr(host, '_on_background_import_committed'):
            root, name = self.root_dir, self.box_name
            task.signals.finished.connect(
                lambda summary: host._on_background_import_committed(root, name, summary)
            )
        task.signals.error.connect(
            lambda message: self._on_image_import_error(idx, task, message)
        )
        QThreadPool.globalInstance().start(task)
        self._notify(f"正在后台保存 {len(sources)} 张照片，仍可继续采集")

    def _on_image_import_progress(
        self,
        idx: int,
        current: int,
        total: int,
        label: str,
    ) -> None:
        if self._page_disposed:
            return
        card = self.cards.get(idx)
        if card is not None:
            card.btn_add.setText(f"后台保存 {current}/{max(total, 1)}")

    def _reset_import_card_button(self, idx: int) -> None:
        if self._page_disposed:
            return
        card = self.cards.get(idx)
        if card is not None:
            card.btn_add.setText("添加图片")

    def _on_image_import_finished(
        self,
        idx: int,
        task: ImageImportTask,
        summary: ImageImportSummary,
    ) -> None:
        if self._image_import_task is task:
            self._image_import_task = None
        if not self._page_disposed:
            self._reset_import_card_button(idx)
            self.reload_from_disk(categories={idx})
            if summary.added_names:
                self.data_changed.emit()
                self.maybe_auto_ocr(idx, list(summary.added_names))
                if len(summary.added_names) == 1:
                    self.focus_next_missing(idx)
                self._notify(f"后台已保存 {len(summary.added_names)} 张照片")
            if (
                not summary.added_names
                and summary.duplicate_count == 1
                and len(summary.duplicate_names) == 1
            ):
                self._notify_duplicate_image(idx, summary.duplicate_names[0])
            elif (
                summary.duplicate_count
                or summary.skipped_count
                or summary.failed_count
            ):
                self._notify(
                    "未添加："
                    f"重复 {summary.duplicate_count}，"
                    f"非图片 {summary.skipped_count}，"
                    f"失败 {summary.failed_count}"
                )
            if summary.cancelled:
                self._notify("后台保存已停止，已完成的照片仍安全保留")
        self._start_next_image_import()
        self._flush_auto_sort_report()

    def _on_image_import_error(
        self,
        idx: int,
        task: ImageImportTask,
        message: str,
    ) -> None:
        if self._image_import_task is task:
            self._image_import_task = None
        if not self._page_disposed:
            self._reset_import_card_button(idx)
            show_operation_error(
                self,
                "后台保存失败",
                "本批照片没有写入元数据；已经写入的新文件已自动回退。",
                str(message),
            )
        self._start_next_image_import()
        self._flush_auto_sort_report()

    def add_files(self, idx, files):
        if self._use_background_image_import():
            self._queue_image_import(
                idx,
                [
                    ("path", Path(source).name, str(source))
                    for source in files
                ],
            )
            return
        self._add_files_sync(idx, files)

    def _add_files_sync(self, idx, files):
        k = str(idx)
        lst = self.meta.setdefault(k, [])

        dirty = False
        skipped = 0
        duplicate = 0
        failed = 0
        added_names = []
        hash_index = self._get_image_hash_index()
        for src in files:
            ext = Path(src).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                log.warning("Skipped non-image file: %s", src)
                skipped += 1
                continue
            try:
                digest = file_sha256(src)
                if digest in hash_index:
                    duplicate += 1
                    continue
                n, dst = self._next_image_path(idx, ext)
                shutil.copy2(src, dst)
                if n not in lst: lst.append(n)
                added_names.append(n)
                hash_index[digest] = n
                dirty = True
            except (OSError, shutil.Error) as e:
                log.warning("Failed to copy %s: %s", src, e)
                failed += 1

        if dirty and not self._commit_added_images(idx, added_names):
            return
        if added_names:
            self._notify(f"已添加 {len(added_names)} 张照片")
        if skipped or duplicate or failed:
            self._notify(f"未添加：重复 {duplicate}，非图片 {skipped}，失败 {failed}")

    def add_image_data(self, idx, image_data):
        if self._use_background_image_import():
            image = self._coerce_qimage(image_data)
            if image is None or image.isNull():
                QMessageBox.warning(self, "无法添加图片", "拖入的数据不是可保存的图片")
                return
            self._queue_image_import(
                idx,
                [("qimage", "剪贴板或拖入图片", QImage(image))],
            )
            return
        self._add_image_data_sync(idx, image_data)

    def _add_image_data_sync(self, idx, image_data):
        image = self._coerce_qimage(image_data)
        if image is None or image.isNull():
            QMessageBox.warning(self, "无法添加图片", "拖入的数据不是可保存的图片")
            return

        payload = self._qimage_jpeg_bytes(image)
        if not payload:
            show_operation_error(
                self,
                "图片处理失败",
                "图片没有保存，当前分类和已有照片未改变。请换一张图片后重试。",
                "无法将拖入或粘贴的图片编码为 JPEG。",
            )
            return
        digest = bytes_sha256(payload)
        hash_index = self._get_image_hash_index()
        if digest in hash_index:
            self._notify_duplicate_image(idx, hash_index[digest])
            return

        k = str(idx)
        lst = self.meta.setdefault(k, [])
        dst_name, dst = self._next_image_path(idx, ".jpg")
        try:
            dst.write_bytes(payload)
        except OSError as exc:
            show_operation_error(
                self,
                "图片保存失败",
                "图片没有写入任务目录，当前分类和已有照片未改变。请检查磁盘空间和目录权限。",
                str(exc),
            )
            return

        if dst_name not in lst:
            lst.append(dst_name)
        if not self._commit_added_images(idx, [dst_name]):
            return
        hash_index[digest] = dst_name
        self.log_telegram_drop(idx, dst_name)
        self._notify("图片已保存")

    def add_virtual_files(self, idx, files):
        if self._use_background_image_import():
            self._queue_image_import(
                idx,
                [
                    ("payload", str(original_name), bytes(payload))
                    for original_name, payload in files
                ],
            )
            return
        self._add_virtual_files_sync(idx, files)

    def _add_virtual_files_sync(self, idx, files):
        k = str(idx)
        lst = self.meta.setdefault(k, [])
        saved = 0
        duplicate = 0
        failed = 0
        saved_names = []
        hash_index = self._get_image_hash_index()
        for original_name, payload in files:
            payload = bytes(payload)
            try:
                source_digest = bytes_sha256(payload)
                if source_digest in hash_index:
                    duplicate += 1
                    continue
                image = QImage.fromData(payload)
                if image.isNull():
                    failed += 1
                    continue
                compact_payload = self._qimage_jpeg_bytes(image)
                if not compact_payload:
                    failed += 1
                    continue
                digest = bytes_sha256(compact_payload)
                if digest in hash_index:
                    duplicate += 1
                    continue
                dst_name, dst = self._next_image_path(idx, ".jpg")
                dst.write_bytes(compact_payload)
                if dst_name not in lst:
                    lst.append(dst_name)
                saved_names.append(dst_name)
                hash_index[digest] = dst_name
                hash_index[source_digest] = dst_name
                saved += 1
            except OSError as e:
                log.warning("Failed to save virtual image %s: %s", original_name, e)
                failed += 1

        if saved and not self._commit_added_images(idx, saved_names):
            return
        if saved:
            log.info("Saved %s virtual dropped image(s) to category %s", saved, idx)
            self._notify(f"已保存 {saved} 张照片")
        if duplicate or failed:
            self._notify(f"未保存：重复 {duplicate}，失败 {failed}")

    # ---------- Auto sort (drop zone above the cards) ----------
    def auto_sort_files(self, files):
        self._auto_sort([("path", Path(source).name, str(source)) for source in files])

    def auto_sort_image(self, image_data):
        image = self._coerce_qimage(image_data)
        if image is None or image.isNull():
            QMessageBox.warning(self, "无法添加图片", "拖入的数据不是可保存的图片")
            return
        self._auto_sort([("qimage", "拖入的图片", QImage(image))])

    def auto_sort_virtual_files(self, files):
        self._auto_sort([("payload", str(name), bytes(payload)) for name, payload in files])

    def _auto_sort(self, sources):
        classifier = get_photo_classifier()
        if classifier is None:
            self._notify("自动分类不可用，请把照片拖到对应的方框")
            return
        if not sources:
            return
        if not self._use_background_image_import():
            self._apply_auto_sort(classifier, classify_import_sources(classifier, self.box_dir, self.meta, sources))
            return
        self._auto_sort_queue.append(list(sources))
        self._start_next_auto_sort()

    def _start_next_auto_sort(self):
        if self._page_disposed or self._auto_sort_task is not None or not self._auto_sort_queue:
            return
        classifier = get_photo_classifier()
        sources = self._auto_sort_queue.pop(0)
        task = PhotoClassifyTask(classifier, self.box_dir, self.meta, sources)
        self._auto_sort_task = task
        task.signals.finished.connect(lambda items: self._on_auto_sort_classified(task, classifier, items))
        task.signals.error.connect(lambda message: self._on_auto_sort_error(task, message))
        self.auto_sort_zone.set_busy(f"正在判断 {len(sources)} 张照片…")
        QThreadPool.globalInstance().start(task)

    def _on_auto_sort_classified(self, task, classifier, items):
        if self._auto_sort_task is task:
            self._auto_sort_task = None
        if self._page_disposed:
            return
        self.auto_sort_zone.set_busy("")
        self._apply_auto_sort(classifier, items)
        self._start_next_auto_sort()

    def _on_auto_sort_error(self, task, message):
        if self._auto_sort_task is task:
            self._auto_sort_task = None
        if self._page_disposed:
            return
        self.auto_sort_zone.set_busy("")
        show_operation_error(self, "自动分类失败", "照片没有保存。可以把照片直接拖到对应的方框。", str(message))
        self._start_next_auto_sort()

    def _queued_import_counts(self) -> dict:
        counts = {}
        for category, batch in self._image_import_queue:
            counts[category] = counts.get(category, 0) + len(batch)
        task = self._image_import_task
        if task is not None:
            counts[task.category] = counts.get(task.category, 0) + len(task.sources)
        return counts

    def _apply_auto_sort(self, classifier, items):
        """Turn predictions into slot imports, asking the user where the rules say so."""
        placements = {}
        replace = set()
        queued = self._queued_import_counts()
        manual = []
        duplicates = []
        skipped = failed = 0
        for item in items:
            if item.error:
                failed += 1
                continue
            if item.duplicate_of:
                duplicates.append(item.duplicate_of)
                continue
            decision = placement_for(item.prediction, classifier.threshold)
            if decision == AUTO_SORT_MANUAL:
                manual.append(item.label)
                continue
            target = item.prediction.category if decision == AUTO_SORT_PLACE else self._ask_auto_sort_category(item)
            if target is None:
                skipped += 1
                continue
            pending = placements.get(target, [])
            existing = 0 if target in replace else len(self.meta.get(str(target), [])) + queued.get(target, 0)
            if existing or pending:
                choice = self._ask_occupied_slot(item, target, existing + len(pending))
                if choice is None:
                    skipped += 1
                    continue
                if choice == "replace":
                    replace.add(target)
                    skipped += len(pending)
                    pending = []
            placements[target] = pending + [item.source]

        placed = {}
        trashed = {}
        for target, sources in sorted(placements.items()):
            if not sources:
                continue
            if target in replace:
                entries = self._trash_slot_photos(target)
                if entries is None:
                    skipped += len(sources)
                    continue
                trashed[target] = entries
            self._import_sources(target, sources)
            placed[target] = len(sources)
        # Background saves post their own toasts; show the sorting summary last
        # so the user sees which slot each photo went to.
        if self._pending_auto_sort_report is not None:
            self._report_auto_sort(*self._pending_auto_sort_report)
        self._pending_auto_sort_report = (placed, manual, duplicates, skipped, failed, trashed)
        self._flush_auto_sort_report()

    def _flush_auto_sort_report(self):
        if self._page_disposed or self._pending_auto_sort_report is None:
            return
        if self._image_import_task is not None or self._image_import_queue:
            return
        report, self._pending_auto_sort_report = self._pending_auto_sort_report, None
        self._report_auto_sort(*report)

    def _ask_auto_sort_category(self, item):
        dialog = AutoSortChoiceDialog(self._auto_sort_preview(item.source), item.label, item.prediction, self)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.chosen_category

    def _ask_occupied_slot(self, item, target, count):
        """Return "append", "replace" or None (skip) for a photo whose slot is taken."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("方框里已有照片")
        box.setText(f"“{self._category_label(target)}”里已经有 {count} 张照片。")
        box.setInformativeText(f"新照片：{item.label}\n替换时，旧照片会移到“最近删除”，可以恢复。")
        append = box.addButton(f"追加为第 {count + 1} 张", QMessageBox.AcceptRole)
        replace = box.addButton("替换旧照片", QMessageBox.DestructiveRole)
        box.addButton("跳过", QMessageBox.RejectRole)
        box.setDefaultButton(append)
        box.exec()
        clicked = box.clickedButton()
        if clicked is append:
            return "append"
        if clicked is replace:
            return "replace"
        return None

    @staticmethod
    def _auto_sort_preview(source) -> QImage:
        kind, _label, data = source
        if kind == "path":
            reader = QImageReader(str(data))
            reader.setAutoTransform(True)
            return reader.read()
        if kind == "payload":
            return QImage.fromData(bytes(data))
        return QImage(data)

    def _trash_slot_photos(self, idx):
        """Move every photo of a slot to the trash; None (and an error dialog) on failure."""
        store = TrashStore(self.root_dir)
        entries = []
        try:
            for filename in list(self.meta.get(str(idx), [])):
                entries.append((store, store.trash_photo(self.box_name, str(idx), filename)))
                self._forget_image_filename(filename)
        except Exception as exc:
            log.warning("Replacing photos of category %s failed: %s", idx, exc)
            self.reload_from_disk(invalidate_hashes=False)
            show_operation_error(
                self,
                "旧照片移除失败",
                f"“{self._category_label(idx)}”的新照片没有保存；已经移走的旧照片可以在“最近删除”里恢复。",
                str(exc),
            )
            return None
        self.reload_from_disk(invalidate_hashes=False)
        return entries

    def _undo_replace(self, trashed):
        """Swap back: the replacing photos go to the trash, the replaced ones return.

        The new photo usually took the old file name, so the old one can only be
        restored after the new one has been moved away.
        """
        if self._page_disposed:
            return
        queued = self._queued_import_counts()
        if any(queued.get(target) for target in trashed):
            self._notify("新照片还在保存，请稍后再撤销", "撤销替换", lambda: self._undo_replace(trashed))
            return
        failures = []
        restored = {}
        for target, entries in trashed.items():
            for filename in list(self.meta.get(str(target), [])):
                try:
                    TrashStore(self.root_dir).trash_photo(self.box_name, str(target), filename)
                    self._forget_image_filename(filename)
                except Exception as exc:
                    failures.append(str(exc))
            for store, entry in entries:
                try:
                    store.restore(entry["id"])
                    restored.setdefault(target, []).append(entry["filename"])
                except Exception as exc:
                    failures.append(str(exc))
        self.reload_from_disk()
        for target, names in restored.items():
            self.maybe_auto_ocr(target, names, force=True)
        if failures:
            show_operation_error(self, "撤销替换失败", "没有恢复的照片仍在“最近删除”中。", "\n".join(failures))
        else:
            self._notify("已撤销替换，新照片移到了最近删除")

    def _import_sources(self, idx, sources):
        if self._use_background_image_import():
            self._queue_image_import(idx, sources)
            return
        paths = [source[2] for source in sources if source[0] == "path"]
        payloads = [(source[1], source[2]) for source in sources if source[0] == "payload"]
        if paths:
            self._add_files_sync(idx, paths)
        if payloads:
            self._add_virtual_files_sync(idx, payloads)
        for source in sources:
            if source[0] == "qimage":
                self._add_image_data_sync(idx, source[2])

    def _report_auto_sort(self, placed, manual, duplicates, skipped, failed, trashed):
        parts = []
        if placed:
            parts.append("已自动放入 " + "，".join(
                f"{self._category_label(idx)} {count} 张" for idx, count in sorted(placed.items())
            ))
        if manual:
            parts.append(f"{len(manual)} 张像 6-8 类（加固/底梁、铅封/其他），请手动拖到对应方框")
        if len(duplicates) == 1 and self._find_image_category(duplicates[0]) is not None:
            parts.append(f"这张照片已在“{self._category_label(self._find_image_category(duplicates[0]))}”，未重复保存")
        elif duplicates:
            parts.append(f"{len(duplicates)} 张已在本箱，未重复保存")
        if skipped:
            parts.append(f"跳过 {skipped} 张")
        if failed:
            parts.append(f"{failed} 张无法读取")
        if not parts:
            return
        text = "；".join(parts)
        if trashed:
            self._notify(text + "；旧照片已移到最近删除", "撤销替换", lambda: self._undo_replace(trashed))
        else:
            self._notify(text)

    def log_telegram_drop(self, idx, dst_name):
        log.info("Saved dropped image to category %s as %s", idx, dst_name)

    def _next_image_path(self, idx, ext):
        base = f"{self.box_name}-{idx}"
        c = 1
        while True:
            name = f"{base}_{c}{ext}" if c > 1 else f"{base}{ext}"
            dst = self.box_dir / name
            if not dst.exists():
                return name, dst
            c += 1

    @staticmethod
    def _coerce_qimage(image_data):
        if isinstance(image_data, QImage):
            return image_data
        if isinstance(image_data, QPixmap):
            return image_data.toImage()
        if hasattr(image_data, "toImage"):
            image = image_data.toImage()
            if isinstance(image, QImage):
                return image
        return None

    @staticmethod
    def _qimage_png_bytes(image: QImage) -> bytes:
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.WriteOnly):
            return b""
        ok = image.save(buffer, "PNG")
        buffer.close()
        return bytes(data) if ok else b""

    @staticmethod
    def _qimage_jpeg_bytes(image: QImage) -> bytes:
        return qimage_jpeg_bytes(image)

    def refresh_card(self, idx):
        c = self.cards[idx]
        for image_widget in c.lw.findChildren(ImageItemWidget):
            image_widget.cancel_async_load()
        c.lw.clear()
        
        lw_w = max(80, c.lw.width())
        lw_h = max(60, int(lw_w * 0.75))
        if c.lw.gridSize().width() > 0:
            lw_w = c.lw.gridSize().width()
            lw_h = c.lw.gridSize().height()
            
        for f in self.meta.get(str(idx), []):
            it = QListWidgetItem()
            it.setSizeHint(QSize(lw_w - 4, lw_h - 4))
            c.lw.addItem(it)
            w = ImageItemWidget(f, self.box_dir, True)
            c.lw.setItemWidget(it, w)
        c.apply_photo_grid()
        c.set_state("complete" if self.meta.get(str(idx), []) else "empty")
        c.lw.update()

    def prepare_for_disposal(self):
        # OCR worker signals are connected through lambdas, which Qt cannot
        # auto-disconnect when this widget dies. The flag makes any late
        # callback exit before touching the half-destroyed page.
        self._page_disposed = True
        self._photo_retry_timer.stop()
        self._pending_auto_ocr = None
        self._image_import_queue.clear()
        self._auto_sort_queue.clear()
        if self._image_import_task is not None:
            self._image_import_task.cancel()
        for image_widget in self.findChildren(ImageItemWidget):
            image_widget.cancel_async_load()

    @staticmethod
    def _menu_style() -> str:
        return MENU_STYLESHEET

    def ctx(self, p, w, idx):
        it = w.itemAt(p)
        if not it: return
        self.select_category(idx)
        w.setFocus(Qt.MouseFocusReason)
        m = styled_menu()
        act_view = m.addAction("查看大图")
        act_view.setIcon(app_icon(self, "view"))
        act_ocr = m.addAction("OCR识别信息")
        act_ocr.setIcon(app_icon(self, "search"))
        act_replace = m.addAction("替换图片")
        act_replace.setIcon(app_icon(self, "replace"))
        move_menu = m.addMenu("移到其他分类")
        move_menu.setIcon(app_icon(self, "sort"))
        move_menu.setStyleSheet(MENU_STYLESHEET)
        move_targets = {}
        for target_idx, label in ALL_CATEGORIES:
            if target_idx == idx:
                continue
            count = len(self.meta.get(str(target_idx), []))
            move_targets[move_menu.addAction(f"{label}（已有 {count} 张）" if count else label)] = target_idx
        m.addSeparator()
        act_del = m.addAction("删除图片")
        act_del.setIcon(app_icon(self, "delete", Theme.ERROR))
        wi = w.itemWidget(it)
        fn = wi.path if wi else it.text()
        chosen = m.exec(w.mapToGlobal(p))
        if chosen == act_view:
            self.preview(it, w, idx)
        elif chosen == act_ocr:
            self.run_ocr_for_image(fn)
        elif chosen in move_targets:
            self._move_image_to_category(fn, idx, move_targets[chosen])
        elif chosen == act_replace:
            fs, _ = QFileDialog.getOpenFileNames(self, "选择替换图片", filter="Images (*.png *.jpg *.jpeg *.bmp)")
            if fs:
                try:
                    shutil.copy2(fs[0], self.box_dir / fn)
                    self._image_hash_index = None
                    _thumb_cache.clear()
                    self.refresh_card(idx)
                    self.ocr_result = {'pending_source': fn, 'verification_status': 'unreadable'}
                    self.save_meta()
                    self.refresh_header_state()
                    self.maybe_auto_ocr(idx, [fn], force=True)
                except (OSError, shutil.Error) as e:
                    log.warning("Replace failed: %s", e)
        elif chosen == act_del:
            try:
                store = TrashStore(self.root_dir)
                entry = store.trash_photo(self.box_name, str(idx), fn)
                self._forget_image_filename(fn)
                self.reload_from_disk(invalidate_hashes=False)
                def undo_photo():
                    try:
                        store.restore(entry["id"])
                        self.reload_from_disk()
                        self._notify("照片已恢复")
                    except Exception as exc:
                        show_operation_error(
                            self,
                            "照片恢复失败",
                            "照片仍保留在最近删除中，没有覆盖现有文件。",
                            str(exc),
                        )
                self._notify("照片已移到最近删除", "撤销", undo_photo)
            except Exception as e:
                log.warning("Delete failed: %s", e)
                show_operation_error(
                    self,
                    "照片删除失败",
                    "照片和分类记录保持原状，请检查工作目录是否可写。",
                    str(e),
                )

    def preview(self, it, w, idx):
        wi = w.itemWidget(it)
        fn = wi.path if wi else it.text()
        photos, index = [], 0
        for category, label in ALL_CATEGORIES:
            for name in self.meta.get(str(category), []):
                if category == idx and name == fn:
                    index = len(photos)
                photos.append((str(self.box_dir / name), f"{label} · {name}"))
        if not photos:
            photos = [(str(self.box_dir / fn), fn)]
        dlg = ImageViewer(str(self.box_dir / fn), self, photos=photos, index=index)
        dlg.exec()

    def update_stat(self):
        miss = []
        for i in self.get_effective_reqs():
            fs = self.meta.get(str(i), [])
            if not fs:
                miss.append(str(i))

        tot = sum(len(v) for v in self.meta.values())
        if miss:
            text = f"缺失 {len(miss)} 项 · {','.join(miss)}"
            apply_status_badge(self.lbl_stat, text, "warn", compact=True)
            self.lbl_stat.setToolTip(f"尚未上传的必填分类：{', '.join(miss)}")
        else:
            apply_status_badge(self.lbl_stat, f"照片齐全 · {tot} 张", "ok", compact=True)
            self.lbl_stat.setToolTip("必填照片已经补齐")

    def update_ocr_status_label(self):
        status = self.ocr_result.get("verification_status", "") if self.ocr_result else ""
        default_status = (
            ("OCR 待识别", "neutral")
            if self.is_auto_ocr_enabled()
            else ("OCR 自动关闭", "neutral")
        )
        text, badge_state = {
            "match": ("箱号一致", "ok"),
            "inferred_match": ("OCR 推算吻合", "warn"),
            "mismatch": ("OCR 箱号不符", "bad"),
            "unreadable": ("OCR 未识别", "warn"),
            "expected_invalid": ("任务箱号异常", "bad"),
            "error": ("OCR 失败", "bad"),
        }.get(status, default_status)
        size_type = self.ocr_result.get("size_type", "") if self.ocr_result else ""
        if size_type:
            text += f" · {size_type}"
        apply_status_badge(self.lbl_ocr_status, text, badge_state, compact=True)
        if status == "inferred_match":
            resolved_number = str(self.ocr_result.get("container_no", ""))
            self.lbl_ocr_status.setToolTip(
                "OCR 已识别箱号前 10 位，最后一位按 ISO 6346 推算；"
                f"最终箱号 {resolved_number} 与任务箱号一致。"
            )
        else:
            self.lbl_ocr_status.setToolTip(
                f"任务箱号：{self.box_name}\nOCR 箱号：{self.ocr_result.get('container_no') or '未识别'}\n"
                f"OCR 箱型：{size_type or '未识别'}\n核对状态仅比较箱号；箱型为识别值"
            )

    def first_image_name(self):
        for idx in (5, 4):
            files = self.meta.get(str(idx), [])
            if files:
                return files[-1]
        for idx in self.get_effective_reqs():
            files = self.meta.get(str(idx), [])
            if files:
                return files[0]
        for idx in range(1, 9):
            files = self.meta.get(str(idx), [])
            if files:
                return files[0]
        return ""

    def run_ocr_from_first_image(self):
        fn = self.first_image_name()
        if not fn:
            QMessageBox.information(self, "没有图片", "请先给当前箱号添加至少一张照片")
            return
        self.run_ocr_for_image(fn)

    def is_auto_ocr_enabled(self) -> bool:
        host = self.window()
        conf = getattr(host, "conf", None)
        if isinstance(conf, dict):
            return bool(conf.get("auto_ocr_enabled", True))
        return True

    def has_trusted_ocr_result(self) -> bool:
        self.ocr_result = current_ocr_result(self.ocr_result, self.box_name, self.box_dir)
        status = self.ocr_result.get("verification_status", "") if self.ocr_result else ""
        return status in {"match", "inferred_match"} and bool(self.ocr_result.get("container_no"))

    def maybe_auto_ocr(self, idx, image_names, force=False):
        if (
            idx not in AUTO_OCR_CATEGORIES
            or not image_names
            or not self.is_auto_ocr_enabled()
        ):
            return
        image_name = image_names[-1]
        if (not force and self.has_trusted_ocr_result()
                and image_name in self.ocr_result.get('source_signatures', {})):
            return
        if self._ocr_task is not None:
            self._pending_auto_ocr = (idx, image_name, bool(force))
            return
        self.run_ocr_for_image(image_name, automatic=True)

    def run_ocr_for_image(self, fn, automatic=False):
        if automatic and not self.is_auto_ocr_enabled():
            return
        path = self.box_dir / fn
        if not path.exists():
            if not automatic:
                show_operation_error(
                    self,
                    "图片不存在",
                    "OCR 没有开始。请返回分类列表刷新或重新添加该图片。",
                    str(path),
                )
            return
        if self._ocr_task is not None:
            if automatic:
                self._pending_auto_ocr = (None, fn, False)
            else:
                QMessageBox.information(self, "OCR识别中", "当前图片仍在识别，请稍候")
            return
        apply_status_badge(self.lbl_ocr_status, "OCR 识别中...", "neutral", compact=True)
        task = OcrTask(str(path))
        task.signals.finished.connect(
            lambda result, image_name=fn, is_auto=automatic: self.on_ocr_finished(result, image_name, is_auto)
        )
        task.signals.error.connect(
            lambda message, image_name=fn, is_auto=automatic: self.on_ocr_error(message, image_name, is_auto)
        )
        self._ocr_automatic = automatic
        self._ocr_task = task
        QThreadPool.globalInstance().start(task)

    def _finish_ocr_task(self):
        self._ocr_task = None
        self._ocr_automatic = False
        pending = self._pending_auto_ocr
        self._pending_auto_ocr = None
        if pending:
            idx, image_name, force = pending
            if idx in AUTO_OCR_CATEGORIES:
                self.maybe_auto_ocr(idx, [image_name], force=force)
            elif self.is_auto_ocr_enabled():
                self.run_ocr_for_image(image_name, automatic=True)

    def on_ocr_finished(self, result, image_name, automatic=False):
        if self._page_disposed:
            return
        # A newer import or deletion can finish while OCR is still running.
        # Never commit that old callback or resurrect its photo list.
        self.reload_from_disk(categories=set())
        if (self._find_image_category(image_name) is None
                or not (self.box_dir / image_name).is_file()
                or (self._pending_auto_ocr and self._pending_auto_ocr[1] != image_name)
                or (self.ocr_result.get('pending_source') and self.ocr_result['pending_source'] != image_name)):
            self._finish_ocr_task()
            return
        if current_ocr_result(result, self.box_name, self.box_dir).get('verification_reason'):
            self._finish_ocr_task()
            self.refresh_header_state()
            return
        result["source_image"] = image_name
        if automatic:
            checked = annotate_ocr_verification(result, self.box_name, automatic=True)
            previous_result = self.ocr_result
            self.ocr_result = checked
            if not self.save_meta():
                self.ocr_result = previous_result
            self.refresh_header_state()
            if checked["verification_status"] == "mismatch":
                self._notify("OCR箱号与任务箱号不一致")
            self._finish_ocr_task()
            return

        dlg = OcrResultDialog(result, self)
        if dlg.exec() == QDialog.Accepted:
            for key in ('source_image', 'source_images', 'source_sha256', 'source_signatures'):
                if key in result:
                    dlg.result_data[key] = result[key]
            previous_result = self.ocr_result
            self.ocr_result = annotate_ocr_verification(dlg.result_data, self.box_name, automatic=False)
            saved = self.save_meta()
            if not saved:
                self.ocr_result = previous_result
            self.refresh_header_state()
            if saved:
                self._notify("OCR结果已保存")
        else:
            self.refresh_header_state()
        self._finish_ocr_task()

    def on_ocr_error(self, message, image_name="", automatic=False):
        if self._page_disposed:
            return
        if automatic:
            existing_status = self.ocr_result.get("verification_status", "") if self.ocr_result else ""
            if existing_status not in {"match", "inferred_match"}:
                previous_result = self.ocr_result
                self.ocr_result = {
                    "verification_status": "error",
                    "error_message": message,
                    "source_image": image_name,
                    "automatic": True,
                    "recognized_at": datetime.now().isoformat(timespec="seconds"),
                }
                if not self.save_meta():
                    self.ocr_result = previous_result
            self.refresh_header_state()
            log.error("Automatic OCR failed for %s: %s", image_name, message)
        else:
            self.refresh_header_state()
            show_operation_error(
                self,
                "OCR 识别失败",
                "照片已经保留，箱号和箱型结果没有被覆盖。可以稍后重新识别。",
                str(message),
            )
        self._finish_ocr_task()

    def open_box_reqs(self):
        dlg = BoxRequirementsDialog(self.reqs, self.box_reqs, self)
        def on_changed(sel):
            previous_reqs = self.box_reqs
            self.box_reqs = sel if sel is not None else None
            if not self.save_meta():
                self.box_reqs = previous_reqs
                return
            effective = self.get_effective_reqs()
            for idx, card in self.cards.items():
                card.set_required(idx in effective)
            self.refresh_header_state()
        dlg.reqs_changed.connect(on_changed)
        dlg.exec()


class RecentDeletedDialog(QDialog):
    changed = Signal()

    def __init__(self, root_dir: Path, parent=None):
        super().__init__(parent)
        self.store = TrashStore(root_dir)
        self.setWindowTitle("最近删除")
        self.resize(640, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = QLabel("最近删除")
        title.setStyleSheet(f"font-size:17px; font-weight:800; color:{Theme.TEXT_MAIN};")
        layout.addWidget(title)

        self.empty_label = QLabel("最近删除为空")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"font-size:14px; color:{Theme.TEXT_MUTED};")
        layout.addWidget(self.empty_label, 1)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.restore_selected())
        self.list.currentItemChanged.connect(lambda *_args: self._update_actions())
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        self.btn_restore = QPushButton("恢复")
        style_button(self.btn_restore, "Primary", "refresh")
        self.btn_restore.clicked.connect(self.restore_selected)
        buttons.addWidget(self.btn_restore)
        self.btn_purge = QPushButton("永久删除")
        style_button(self.btn_purge, "Danger", icon_name="delete")
        self.btn_purge.clicked.connect(self.purge_selected)
        buttons.addWidget(self.btn_purge)
        self.btn_clear = QPushButton("清空全部")
        style_button(self.btn_clear, "Danger", icon_name="delete")
        self.btn_clear.clicked.connect(self.clear_all)
        buttons.addWidget(self.btn_clear)
        buttons.addStretch()
        close = QPushButton("关闭")
        style_button(close)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self):
        self.list.clear()
        entries = self.store.list_entries()
        for entry in entries:
            kind = "任务" if entry.get("kind") == "task" else "照片"
            detail = str(entry.get("box_name", ""))
            if entry.get("kind") == "photo":
                detail += f" / 分类 {entry.get('category')} / {entry.get('filename')}"
            deleted_at = str(entry.get("deleted_at", ""))
            try:
                deleted_at = datetime.fromisoformat(deleted_at).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            item = QListWidgetItem(f"{kind}  {detail}    {deleted_at}")
            item.setData(Qt.UserRole, entry.get("id"))
            self.list.addItem(item)
        if entries:
            self.list.setCurrentRow(0)
        self.empty_label.setVisible(not entries)
        self.list.setVisible(bool(entries))
        self.btn_clear.setEnabled(bool(entries))
        self._update_actions()

    def _update_actions(self):
        has_selection = self.list.currentItem() is not None
        self.btn_restore.setEnabled(has_selection)
        self.btn_purge.setEnabled(has_selection)

    def _selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def restore_selected(self):
        entry_id = self._selected_id()
        if not entry_id:
            return
        try:
            self.store.restore(str(entry_id))
        except (FileExistsError, FileNotFoundError, OSError, ValueError, KeyError) as exc:
            show_operation_error(
                self,
                "恢复失败",
                "内容仍保留在最近删除中，没有覆盖同名任务或照片。",
                str(exc),
            )
            return
        self.changed.emit()
        self.reload()

    def purge_selected(self):
        entry_id = self._selected_id()
        if not entry_id:
            return
        if QMessageBox.question(self, "永久删除", "永久删除所选内容？此操作无法撤销。") != QMessageBox.Yes:
            return
        try:
            self.store.purge(str(entry_id))
        except (OSError, KeyError) as exc:
            show_operation_error(
                self,
                "永久删除失败",
                "所选内容仍保留在最近删除中，请检查目录权限后重试。",
                str(exc),
            )
            return
        self.changed.emit()
        self.reload()

    def clear_all(self):
        if QMessageBox.question(self, "清空最近删除", "永久删除回收区中的全部内容？") != QMessageBox.Yes:
            return
        try:
            self.store.clear()
        except OSError as exc:
            show_operation_error(
                self,
                "清空最近删除失败",
                "未能清理的内容仍保留在最近删除中。",
                str(exc),
            )
            return
        self.changed.emit()
        self.reload()


class DataHealthDialog(QDialog):
    def __init__(self, root_dir: Path, parent=None):
        super().__init__(parent)
        self.root_dir = Path(root_dir)
        self.report: HealthReport | None = None
        self.selected_task_name = ""
        self._error_details = ""
        self._task = None
        self.setWindowTitle("数据检查")
        self.resize(760, 560)
        self.setMinimumSize(560, 420)
        self.setup_ui()
        QTimer.singleShot(0, self.start_check)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("数据检查")
        title.setStyleSheet(f"font-size:18px; font-weight:800; color:{Theme.TEXT_MAIN};")
        layout.addWidget(title)
        root_label = QLabel(str(self.root_dir))
        root_label.setWordWrap(True)
        root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root_label.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        layout.addWidget(root_label)

        self.summary = QLabel("正在只读检查工作目录，不会修改照片或元数据。")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"font-size:13px; color:{Theme.TEXT_SEC};")
        layout.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        layout.addWidget(self.progress)

        self.results = QTreeWidget()
        self.results.setColumnCount(3)
        self.results.setHeaderLabels(["级别", "检查项目", "说明"])
        self.results.setRootIsDecorated(False)
        self.results.setAlternatingRowColors(True)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.results.itemDoubleClicked.connect(self.open_issue_task)
        layout.addWidget(self.results, 1)

        buttons = QHBoxLayout()
        self.btn_copy_health = QPushButton("复制结果")
        style_button(self.btn_copy_health, icon_name="open")
        self.btn_copy_health.setEnabled(False)
        self.btn_copy_health.clicked.connect(self.copy_report)
        buttons.addWidget(self.btn_copy_health)
        self.btn_open_logs = QPushButton("打开日志目录")
        style_button(self.btn_open_logs, icon_name="folder")
        self.btn_open_logs.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))
        )
        buttons.addWidget(self.btn_open_logs)
        buttons.addStretch()
        self.btn_cancel_health = QPushButton("取消检查")
        style_button(self.btn_cancel_health)
        self.btn_cancel_health.clicked.connect(self.cancel_check)
        buttons.addWidget(self.btn_cancel_health)
        close = QPushButton("关闭")
        style_button(close, "Primary")
        close.clicked.connect(self.accept)
        self.btn_close_health = close
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def start_check(self):
        if self._task is not None:
            return

        def valid_task_name(name: str) -> bool:
            declared = extract_declared_container_no(name)
            return bool(declared and validate_container_no(declared))

        task = HealthCheckTask(self.root_dir, valid_task_name, get_photo_classifier())
        task.signals.progress.connect(self.on_progress)
        task.signals.finished.connect(self.on_finished)
        task.signals.cancelled.connect(self.on_cancelled)
        task.signals.error.connect(self.on_error)
        self._task = task
        self.btn_cancel_health.setEnabled(True)
        QThreadPool.globalInstance().start(task)

    def cancel_check(self):
        if self._task is None:
            return
        self.summary.setText("正在取消数据检查...")
        self.btn_cancel_health.setEnabled(False)
        self._task.cancel()

    def on_progress(self, current: int, total: int, label: str):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(current, max(1, total)))
        self.summary.setText(f"{label}，不会修改照片或元数据。")

    def on_finished(self, report: HealthReport):
        self._task = None
        self.report = report
        self._error_details = ""
        self.progress.hide()
        self.btn_cancel_health.setEnabled(False)
        self.results.clear()
        classified = (
            f"模型已核对 {report.classified_photo_count} 张照片的类别。"
            if report.classified_photo_count else ""
        )
        if report.issues:
            self.summary.setText(
                f"检查完成：{report.task_count} 个任务，发现 "
                f"{report.error_count} 个错误、{report.warning_count} 个警告。"
                f"{classified}双击带箱号的问题可打开该任务。"
            )
            severity_labels = {"error": "错误", "warning": "警告", "info": "提示"}
            severity_colors = {
                "error": QColor(Theme.ERROR),
                "warning": QColor(Theme.WARNING),
                "info": QColor(Theme.TEXT_SEC),
            }
            for issue in report.issues:
                detail = f"{issue.task_name} · {issue.detail}" if issue.task_name else issue.detail
                item = QTreeWidgetItem([
                    severity_labels.get(issue.severity, issue.severity),
                    issue.title,
                    detail,
                ])
                item.setForeground(0, QBrush(severity_colors.get(issue.severity, QColor(Theme.TEXT_SEC))))
                item.setToolTip(2, detail)
                item.setData(0, Qt.UserRole, issue.task_name)
                self.results.addTopLevelItem(item)
        else:
            checked_at = report.checked_at.replace("T", " ")
            self.summary.setText(
                f"检查完成：{report.task_count} 个任务、"
                f"{report.referenced_photo_count} 张已引用照片，未发现问题。"
                f"{classified}检查时间 {checked_at}。"
            )
            item = QTreeWidgetItem(["正常", "未发现问题", f"检查耗时 {report.duration_ms} ms"])
            item.setForeground(0, QBrush(QColor(Theme.SUCCESS)))
            self.results.addTopLevelItem(item)
        self.btn_copy_health.setEnabled(True)

    def on_error(self, message: str):
        self._task = None
        self._error_details = str(message)
        self.progress.hide()
        self.btn_cancel_health.setEnabled(False)
        self.summary.setText("数据检查未完成，请打开日志目录查看详细记录。")
        self.results.clear()
        self.results.addTopLevelItem(QTreeWidgetItem(["错误", "检查失败", message[:300]]))
        self.btn_copy_health.setEnabled(True)

    def on_cancelled(self):
        self._task = None
        self.progress.hide()
        self.btn_cancel_health.setEnabled(False)
        self.summary.setText("数据检查已取消，未修改任何照片或元数据。")
        self.results.clear()
        self.results.addTopLevelItem(QTreeWidgetItem(["提示", "检查已取消", "可以关闭窗口后重新开始"]))

    def open_issue_task(self, item: QTreeWidgetItem, _column: int = 0):
        task_name = item.data(0, Qt.UserRole)
        if not task_name or not (self.root_dir / str(task_name)).is_dir():
            return
        self.selected_task_name = str(task_name)
        self.accept()

    def copy_report(self):
        if self.report is None and not self._error_details:
            return
        if self.report is not None:
            text = self.report.to_text()
        else:
            text = (
                f"{APP_DISPLAY_NAME} 数据检查失败\n"
                f"工作目录：{self.root_dir}\n"
                f"详细信息：{self._error_details}"
            )
        QApplication.clipboard().setText(text)
        self.btn_copy_health.setText("已复制")
        QTimer.singleShot(1500, lambda: self.btn_copy_health.setText("复制结果"))

    def closeEvent(self, event):
        if self._task is not None:
            self._task.cancel()
        super().closeEvent(event)


class CollectionDashboardPage(QWidget):
    export_requested = Signal()
    refresh_requested = Signal()
    task_open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_solid_background(self, Theme.BG_MAIN)
        self.report = build_collection_report([], ALL_CATEGORIES)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("采集看板")
        title.setStyleSheet(f"font-size:18px; font-weight:800; color:{Theme.TEXT_MAIN};")
        self.subtitle = ElidedLabel("尚未选择工作目录", auto_tooltip=False)
        self.subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.btn_refresh = QToolButton()
        self.btn_refresh.setIcon(app_icon(self.btn_refresh, "refresh", Theme.TEXT_SEC))
        self.btn_refresh.setToolTip("刷新看板")
        self.btn_refresh.setFixedSize(34, 34)
        style_command_button(self.btn_refresh)
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        header.addWidget(self.btn_refresh)

        self.btn_export = QPushButton("导出 Excel")
        style_button(self.btn_export, "Primary", "export")
        self.btn_export.setFixedHeight(34)
        self.btn_export.clicked.connect(self.export_requested.emit)
        header.addWidget(self.btn_export)
        layout.addLayout(header)

        self.report_summary = QFrame()
        self.report_summary.setObjectName("reportSummary")
        self.report_summary.setStyleSheet(
            f"QFrame#reportSummary {{ background:{Theme.BG_SUBTLE}; "
            f"border:1px solid {Theme.BORDER}; border-radius:8px; }}"
        )
        self.summary_layout = QGridLayout(self.report_summary)
        self.summary_layout.setContentsMargins(14, 10, 14, 10)
        self.summary_layout.setHorizontalSpacing(18)
        self.summary_layout.setVerticalSpacing(8)
        self.kpi_values = {}
        self.kpi_widgets = []
        kpis = (
            ("total", "总任务", Theme.TEXT_MAIN),
            ("complete", "照片齐全", Theme.SUCCESS),
            ("pending", "缺照片", Theme.ERROR),
            ("anomaly", "待核验/异常", Theme.WARNING),
            ("photos", "照片总数", Theme.TEXT_MAIN),
            ("rate", "必填项完成率", Theme.SUCCESS),
        )
        for key, label_text, color in kpis:
            stat = QWidget()
            stat_layout = QVBoxLayout(stat)
            stat_layout.setContentsMargins(0, 0, 0, 0)
            stat_layout.setSpacing(1)
            value = QLabel("0")
            value.setStyleSheet(f"font-size:19px; font-weight:800; color:{color};")
            label = QLabel(label_text)
            label.setStyleSheet(f"font-size:11px; color:{Theme.TEXT_MUTED};")
            stat_layout.addWidget(value)
            stat_layout.addWidget(label)
            self.kpi_widgets.append(stat)
            self.kpi_values[key] = value
        layout.addWidget(self.report_summary)

        category_title = QHBoxLayout()
        category_label = QLabel("八类照片完成度")
        category_label.setStyleSheet(f"font-size:14px; font-weight:700; color:{Theme.TEXT_MAIN};")
        category_title.addWidget(category_label)
        category_title.addStretch()
        self.category_hint = QLabel()
        self.category_hint.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        category_title.addWidget(self.category_hint)
        layout.addLayout(category_title)

        self.category_table = QTableWidget(0, 6)
        self.category_table.setHorizontalHeaderLabels(("照片分类", "必填箱", "已收集箱", "缺失箱", "已上传", "完成率"))
        self.category_table.verticalHeader().hide()
        self.category_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.category_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.category_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.category_table.setFocusPolicy(Qt.NoFocus)
        self.category_table.setFixedHeight(274)
        self.category_table.setStyleSheet(self._table_style())
        category_header = self.category_table.horizontalHeader()
        category_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            category_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        category_header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.category_table.setColumnWidth(5, 130)
        layout.addWidget(self.category_table)

        self.details_header = QGridLayout()
        self.details_header.setContentsMargins(0, 0, 0, 0)
        self.details_header.setHorizontalSpacing(10)
        self.details_header.setVerticalSpacing(6)
        self.details_title_group = QWidget()
        details_title_layout = QHBoxLayout(self.details_title_group)
        details_title_layout.setContentsMargins(0, 0, 0, 0)
        details_title_layout.setSpacing(8)
        details_title = QLabel("箱号缺项明细")
        details_title.setStyleSheet(f"font-size:14px; font-weight:700; color:{Theme.TEXT_MAIN};")
        details_title_layout.addWidget(details_title)
        self.issue_count = QLabel()
        self.issue_count.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        details_title_layout.addWidget(self.issue_count)
        details_title_layout.addStretch()
        self.details_controls = QWidget()
        details_controls_layout = QHBoxLayout(self.details_controls)
        details_controls_layout.setContentsMargins(0, 0, 0, 0)
        details_controls_layout.setSpacing(8)
        self.only_issues = QCheckBox("仅显示缺照片")
        self.only_issues.setChecked(True)
        self.only_issues.toggled.connect(self._render_details)
        details_controls_layout.addWidget(self.only_issues)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索箱号、箱型或缺失项")
        self.search.setMinimumWidth(0)
        self.search.setMaximumWidth(260)
        self.search.setFixedHeight(34)
        self.search.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._details_debounce = QTimer(self)
        self._details_debounce.setSingleShot(True)
        self._details_debounce.setInterval(120)
        self._details_debounce.timeout.connect(self._render_details)
        self.search.textChanged.connect(
            lambda _text: self._details_debounce.start()
        )
        details_controls_layout.addWidget(self.search, 1)
        layout.addLayout(self.details_header)

        self.details_table = QTableWidget(0, 7)
        self.details_table.setHorizontalHeaderLabels(("箱号", "箱型", "照片状态", "照片", "缺失照片", "OCR/箱号核验", "最后更新"))
        self.details_table.verticalHeader().hide()
        self.details_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.details_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.details_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.details_table.setAlternatingRowColors(False)
        self.details_table.setStyleSheet(self._table_style())
        details_table_header = self.details_table.horizontalHeader()
        for column in (0, 1, 2, 3, 5, 6):
            details_table_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        details_table_header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.details_table.cellDoubleClicked.connect(self._open_row)
        layout.addWidget(self.details_table, 1)

        self.setTabOrder(self.btn_refresh, self.btn_export)
        self.setTabOrder(self.btn_export, self.only_issues)
        self.setTabOrder(self.only_issues, self.search)
        self.setTabOrder(self.search, self.details_table)
        self._responsive_compact = None
        self._apply_responsive_layout()
        self.set_report(self.report, "")

    def _apply_responsive_layout(self):
        compact = self.width() < 760
        if compact == self._responsive_compact:
            return

        columns = 3 if compact else 6
        for widget in self.kpi_widgets:
            self.summary_layout.removeWidget(widget)
        for column in range(6):
            self.summary_layout.setColumnStretch(column, 0)
        for position, widget in enumerate(self.kpi_widgets):
            row, column = divmod(position, columns)
            self.summary_layout.addWidget(widget, row, column)
            self.summary_layout.setColumnStretch(column, 1)

        self.details_header.removeWidget(self.details_title_group)
        self.details_header.removeWidget(self.details_controls)
        for column in range(2):
            self.details_header.setColumnStretch(column, 0)
        if compact:
            self.details_header.addWidget(self.details_title_group, 0, 0)
            self.details_header.addWidget(self.details_controls, 1, 0)
        else:
            self.details_header.addWidget(self.details_title_group, 0, 0)
            self.details_header.addWidget(self.details_controls, 0, 1)
            self.details_header.setColumnStretch(0, 1)

        for column in (1, 4):
            self.category_table.setColumnHidden(column, compact)
        for column in (3, 6):
            self.details_table.setColumnHidden(column, compact)
        self.category_table.setColumnWidth(5, 100 if compact else 130)
        self._responsive_compact = compact

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "summary_layout"):
            self._apply_responsive_layout()

    @staticmethod
    def _table_style() -> str:
        return f"""
            QTableWidget {{
                background:{Theme.BG_CARD};
                border:1px solid {Theme.BORDER};
                border-radius:8px;
                gridline-color:{Theme.BORDER};
                selection-background-color:{Theme.BG_ACTIVE};
                selection-color:{Theme.TEXT_MAIN};
            }}
            QHeaderView::section {{
                background:{Theme.BG_SUBTLE};
                color:{Theme.TEXT_SEC};
                border:none;
                border-bottom:1px solid {Theme.BORDER};
                padding:7px 8px;
                font-size:12px;
                font-weight:700;
            }}
        """

    @staticmethod
    def _table_item(text, alignment=Qt.AlignLeft | Qt.AlignVCenter):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(alignment)
        return item

    def set_report(self, report: CollectionReport, root_label: str) -> None:
        self.report = report
        self.btn_export.setEnabled(report.total_tasks > 0)
        self.subtitle.setText(f"工作目录：{root_label}" if root_label else "尚未选择工作目录")
        self.subtitle.setToolTip(root_label)
        self.kpi_values["total"].setText(str(report.total_tasks))
        self.kpi_values["complete"].setText(str(report.photo_complete_tasks))
        self.kpi_values["pending"].setText(str(report.missing_photo_tasks))
        self.kpi_values["anomaly"].setText(str(report.verification_issue_tasks))
        self.kpi_values["photos"].setText(str(report.total_photos))
        self.kpi_values["rate"].setText(f"{report.photo_completion_rate:.0%}")
        self.category_hint.setText(f"共 {report.required_slots} 个必填照片项")

        for row_index in range(self.category_table.rowCount()):
            for column in range(self.category_table.columnCount()):
                old_widget = self.category_table.cellWidget(row_index, column)
                if old_widget is not None:
                    self.category_table.removeCellWidget(row_index, column)
                    old_widget.hide()
                    old_widget.deleteLater()
        self.category_table.setRowCount(len(report.categories))
        for row_index, category in enumerate(report.categories):
            values = (
                f"{category.index}  {category.label}",
                category.required_tasks,
                category.collected_tasks,
                category.missing_tasks,
                category.uploaded_photos,
            )
            for column, value in enumerate(values):
                alignment = Qt.AlignLeft | Qt.AlignVCenter if column == 0 else Qt.AlignCenter
                item = self._table_item(value, alignment)
                if column == 3 and category.missing_tasks:
                    item.setForeground(QBrush(QColor(Theme.ERROR)))
                    item.setBackground(QBrush(QColor(Theme.ERROR_SOFT)))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.category_table.setItem(row_index, column, item)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(round(category.completion_rate * 100))
            progress.setFormat("%p%" if category.required_tasks else "非必填")
            progress.setAlignment(Qt.AlignCenter)
            progress.setFixedHeight(18)
            chunk_color = Theme.PRIMARY_SOFT if category.required_tasks else Theme.BG_SUBTLE
            progress.setStyleSheet(
                f"QProgressBar {{ background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; "
                f"border-radius:4px; color:{Theme.TEXT_SEC}; font-size:11px; }}"
                f"QProgressBar::chunk {{ background:{chunk_color}; border-radius:3px; }}"
            )
            self.category_table.setCellWidget(row_index, 5, progress)
            self.category_table.setRowHeight(row_index, 30)
        self._render_details()

    def _render_details(self, *_args) -> None:
        key = self.search.text().strip().lower()
        rows = (
            [row for row in self.report.rows if not row.photo_complete]
            if self.only_issues.isChecked()
            else self.report.rows
        )
        filtered = [
            row for row in rows
            if not key or key in " ".join((
                row.name,
                row.size_type,
                " ".join(row.missing_labels),
                TASK_STATUS_LABELS.get(row.task_state, row.task_state),
                "照片齐全" if row.photo_complete else "缺照片",
                OCR_STATUS_LABELS.get(row.ocr_status, row.ocr_status),
            )).lower()
        ]
        self.details_table.setRowCount(len(filtered))
        self.issue_count.setText(f"{self.report.missing_photo_tasks} 个箱号缺照片")
        for row_index, report_row in enumerate(filtered):
            missing_text = "、".join(report_row.missing_labels)
            if not missing_text:
                missing_text = "无"
            values = (
                report_row.name,
                report_row.size_type or "-",
                "照片齐全" if report_row.photo_complete else "缺照片",
                report_row.photo_count,
                missing_text,
                OCR_STATUS_LABELS.get(report_row.ocr_status, report_row.ocr_status or "待识别"),
                report_row.last_updated[:16],
            )
            for column, value in enumerate(values):
                alignment = Qt.AlignCenter if column in {1, 2, 3} else Qt.AlignLeft | Qt.AlignVCenter
                item = self._table_item(value, alignment)
                if column == 0:
                    item.setData(Qt.UserRole, report_row.name)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column == 2:
                    color = Theme.SUCCESS if report_row.photo_complete else Theme.ERROR
                    item.setForeground(QBrush(QColor(color)))
                if column == 4 and report_row.missing_labels:
                    item.setForeground(QBrush(QColor(Theme.ERROR)))
                    item.setBackground(QBrush(QColor(Theme.ERROR_SOFT)))
                if column == 5 and report_row.verification_issue:
                    item.setForeground(QBrush(QColor(Theme.WARNING)))
                    item.setBackground(QBrush(QColor(Theme.WARNING_SOFT)))
                self.details_table.setItem(row_index, column, item)
            self.details_table.setRowHeight(row_index, 34)

    def _open_row(self, row: int, _column: int) -> None:
        item = self.details_table.item(row, 0)
        if item is not None:
            box_name = item.data(Qt.UserRole)
            if box_name:
                self.task_open_requested.emit(str(box_name))


# ---------- Main Application ----------
class ContainerApp(QMainWindow):
    # 侧边栏自动折叠阈值
    SIDEBAR_COLLAPSE_THRESHOLD = 1080
    TASK_ROW_HEIGHT = 64
    # Keep the first paint small enough for large work directories; the
    # remaining rows are materialized in the next event-loop turn.
    INITIAL_TASK_WIDGETS = 12
    TASK_RENDER_BUDGET_MS = 14
    TASK_DATA_ROLE = int(Qt.UserRole) + 1

    def __init__(self):
        super().__init__()
        self._closing = False
        self._config_path = Path(CONFIG_PATH)
        self._sidebar_collapsed = False
        self._sidebar_user_collapsed = False
        self._sidebar_auto_collapsed = False
        self._drag_pos = None
        self._drag_press_global = None
        self._drag_restore_ratio = 0.5
        self._export_task = None
        self._export_progress = None
        self._export_root = None
        self._export_kind = None
        self._excel_export_task_count = 0
        self._batch_ocr_task = None
        self._batch_ocr_root = None
        self.task_data = []
        self._dirty_task_names = set()
        self._report_dirty = True
        self._windows_snap_initialized = False
        self._native_maximize_pressed = False
        self._work_root_probe_generation = 0
        self._verified_remote_root = None
        self._task_scan_generation = 0
        self._task_scan_task = None
        self._task_scan_tasks = {}
        self._render_generation = 0
        self._update_check_task = None
        self._update_download_task = None
        self._update_thread_pool = QThreadPool(self)
        self._update_thread_pool.setMaxThreadCount(2)
        self._update_progress = None
        self._available_update = None
        self._pending_install_plan = None
        self._installing_update = False
        self._last_update_toast_release_id = ""
        self._update_notification_stop = threading.Event()
        self._update_notification_thread = None
        self._update_notification_bridge = UpdateNotificationBridge(self)
        self._update_notification_bridge.release_ready.connect(
            self._on_update_notification
        )
        self._work_root_probe_signals = WorkRootProbeSignals(self)
        self._work_root_probe_signals.finished.connect(self._on_work_root_probe_finished)
        self._authorization_service = AuthorizationService(self._config_path)
        self._authorization_task = None
        self._authorization_decision = AuthorizationDecision(
            AuthStatus.DISABLED if not self._authorization_service.enabled else AuthStatus.REQUIRES_ONLINE,
            not self._authorization_service.enabled,
        )
        self._authorization_ready = not self._authorization_service.enabled
        self.setWindowTitle(f"{APP_DISPLAY_NAME} v{APP_VERSION}")
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
        )
        self.resize(1280, 850)
        self.setMinimumSize(400, 400)
        try:
            window_icon_path = resource_path(APP_LOGO_ICO)
            if window_icon_path.exists():
                self.setWindowIcon(QIcon(str(window_icon_path)))
        except Exception:
            pass
        self.load_conf()
        if Theme.NAME != self.conf["theme_name"]:
            apply_theme(self.conf["theme_name"])
            app = QApplication.instance()
            if app is not None:
                configure_app_style(app)
        self.setup_ui()
        self.toast = ActionToast(self)
        self.collection_report = self.report_page.report
        self.refresh()
        self._thumbnail_trim_timer = QTimer(self)
        self._thumbnail_trim_timer.setInterval(10 * 60 * 1000)
        self._thumbnail_trim_timer.timeout.connect(
            lambda: _thumb_cache.trim_to_fraction(0.5)
        )
        self._thumbnail_trim_timer.start()
        self._update_check_timer = QTimer(self)
        self._update_check_timer.setInterval(5 * 60 * 1000)
        self._update_check_timer.timeout.connect(
            lambda: self.check_for_updates(manual=False)
        )
        self._authorization_timer = QTimer(self)
        self._authorization_timer.setSingleShot(True)
        self._authorization_timer.timeout.connect(self.check_authorization)
        if QApplication.platformName().lower() != "offscreen":
            self._update_check_timer.start()
            if self._authorization_service.enabled:
                next_check = self._authorization_service.seconds_until_next_check()
                cached = self._authorization_service.cached_decision(next_check)
                if cached is not None:
                    self._authorization_decision = cached
                    self._authorization_ready = True
                    self._schedule_next_authorization_check()
                else:
                    QTimer.singleShot(1200, self.check_authorization)
            QTimer.singleShot(
                2500,
                lambda: self.check_for_updates(manual=False),
            )
            self._start_update_notification_listener()
        # Warm the OCR engine shortly after startup so the first closed-door
        # photo is recognized in a fraction of the cold-start time. Skipped on
        # the offscreen platform (tests / acceptance drive OCR explicitly).
        QTimer.singleShot(1500, self._prewarm_ocr_engine)

    @property
    def authorization_ready(self) -> bool:
        """Whether the startup health marker may be committed."""
        return self._authorization_ready

    def _schedule_next_authorization_check(self):
        delay = self._authorization_service.seconds_until_next_check()
        self._authorization_timer.start(delay * 1000)

    def check_authorization(self):
        if self._closing or not self._authorization_service.enabled:
            self._authorization_ready = True
            return
        if self._authorization_task is not None:
            return
        task = AuthorizationTask(self._authorization_service, APP_VERSION)
        self._authorization_task = task
        task.signals.finished.connect(self._on_authorization_finished)
        self._update_thread_pool.start(task)

    def _on_authorization_finished(self, decision: AuthorizationDecision):
        self._authorization_task = None
        self._authorization_decision = decision
        self._authorization_ready = bool(decision.allowed)
        if decision.allowed:
            if QApplication.platformName().lower() != "offscreen":
                self._schedule_next_authorization_check()
            return
        if self._closing:
            return
        QMessageBox.warning(
            self,
            "软件已过期",
            decision.message or "软件已过期，请联系管理员。",
        )
        self.close()

    def _require_authorization_for_action(self) -> bool:
        if not self._authorization_service.enabled:
            return True
        if self._authorization_ready and self._authorization_decision.allowed:
            return True
        if self._authorization_decision.status == AuthStatus.REQUIRES_ONLINE:
            self.show_toast("授权检查未完成，请连接网络后重试")
        else:
            self.show_toast("软件已过期，请联系管理员")
        return False

    def _prewarm_ocr_engine(self):
        if not bool(self.conf.get("auto_ocr_enabled", True)):
            return
        if QApplication.platformName().lower() == "offscreen":
            return

        def warm():
            try:
                ensure_ocr_engine()
                log.info("OCR engine prewarmed")
            except Exception:
                log.exception("OCR engine prewarm failed")

        threading.Thread(
            target=warm,
            name="PhotoCollectOcrPrewarm",
            daemon=True,
        ).start()

    def load_conf(self):
        config_path = self._config_path
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Unable to create configuration directory: %s", exc)
        self.conf = {
            "root": "",
            "hist": [],
            "dashboard_filter": "all",
            "sort_order": "status",
            "sidebar_collapsed": False,
            "auto_ocr_enabled": True,
            "theme_name": DEFAULT_THEME_NAME,
            "zip_name_template": DEFAULT_EXPORT_TEMPLATES["zip"],
            "excel_name_template": DEFAULT_EXPORT_TEMPLATES["excel"],
        }
        if config_path.exists():
            try:
                d = json.loads(config_path.read_text(encoding='utf-8'))
                if isinstance(d, dict):
                    for key in self.conf:
                        if key in d:
                            self.conf[key] = d[key]
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Unable to load configuration: %s", exc)
        if self.conf["dashboard_filter"] not in {"all", "pending", "anomaly", "complete"}:
            self.conf["dashboard_filter"] = "all"
        if self.conf["sort_order"] not in {"status", "name", "updated"}:
            self.conf["sort_order"] = "status"
        if not isinstance(self.conf["hist"], list):
            self.conf["hist"] = []
        self.conf["hist"] = [str(value) for value in self.conf["hist"] if isinstance(value, str)][:10]
        if not isinstance(self.conf["root"], str):
            self.conf["root"] = ""
        if not isinstance(self.conf["auto_ocr_enabled"], bool):
            self.conf["auto_ocr_enabled"] = True
        self.conf["theme_name"] = normalize_theme_name(self.conf.get("theme_name"))
        for kind, key in (
            ("zip", "zip_name_template"),
            ("excel", "excel_name_template"),
        ):
            template = self.conf.get(key)
            if not isinstance(template, str) or not template.strip() or len(template) > 160:
                self.conf[key] = DEFAULT_EXPORT_TEMPLATES[kind]
                continue
            rendered = render_export_filename(
                kind,
                template,
                task_count=1,
                photo_count=1,
                scope="全部任务",
            )
            if rendered.used_fallback:
                self.conf[key] = DEFAULT_EXPORT_TEMPLATES[kind]
        self._sidebar_user_collapsed = bool(self.conf["sidebar_collapsed"])
        self._sidebar_auto_collapsed = False
        self._sidebar_collapsed = self._sidebar_user_collapsed
        self._current_filter = self.conf["dashboard_filter"]
        self._sort_order = self.conf["sort_order"]
        self.root = Path(self.conf["root"]) if self.conf["root"] else None
        self._work_root_state = "unselected"

    def save_conf(self):
        config_path = self._config_path
        self.conf["root"] = str(self.root) if self.root else ""
        self.conf["dashboard_filter"] = self._current_filter
        self.conf["sort_order"] = self._sort_order
        self.conf["sidebar_collapsed"] = self._sidebar_user_collapsed
        if self.root:
            s = str(self.root)
            h = self.conf["hist"]
            if s in h:
                h.remove(s)
            h.insert(0, s)
            self.conf["hist"] = h[:10]
        tmp = config_path.with_suffix(".tmp")
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.conf, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, config_path)
        except OSError as exc:
            log.warning("Unable to save configuration: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def _pending_batch_ocr_tasks(self) -> list:
        if not self.root or not self.root.is_dir():
            return []
        all_meta = read_meta(self.root / META_NAME)
        global_reqs = all_meta.get("__requirements__", DEFAULT_REQS)
        if not isinstance(global_reqs, list):
            global_reqs = DEFAULT_REQS
        pending = []
        for summary in self.task_data:
            box_name = str(summary.get("name", ""))
            if not box_name:
                continue
            box_meta = all_meta.get(box_name, {})
            if not isinstance(box_meta, dict):
                box_meta = {}
            existing_ocr = box_meta.get("__ocr__", {})
            if not isinstance(existing_ocr, dict):
                existing_ocr = {}
            existing_ocr = current_ocr_result(existing_ocr, box_name, self.root / box_name)
            if (existing_ocr.get('verification_status') in {'match', 'inferred_match'}
                    and existing_ocr.get('source_signatures') and important_ocr_complete(existing_ocr)):
                continue
            box_dir = self.root / box_name
            photo_state = resolve_task_photos(
                box_dir,
                box_meta,
                global_reqs,
                category_count=len(ALL_CATEGORIES),
                auto_ocr_categories=AUTO_OCR_CATEGORIES,
            )
            image_paths = []
            for category in (5, 4):
                candidates = [
                    photo.path
                    for photo in photo_state.photos
                    if photo.category == category
                ]
                if candidates:
                    image_paths.append(candidates[-1])
            if image_paths:
                pending.append({
                    "name": box_name,
                    "image_paths": tuple(image_paths),
                })
        return pending

    def start_batch_ocr(self):
        if self._batch_ocr_task is not None:
            self._batch_ocr_task.cancel()
            self.btn_batch_ocr.setText("正在停止...")
            self.btn_batch_ocr.setEnabled(False)
            return
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请先选择工作目录")
            return
        self.refresh()
        pending = self._pending_batch_ocr_tasks()
        if not pending:
            self.show_toast("没有需要识别的半关门或全关门照片")
            return
        task = BatchOcrRunner(pending, self.root)
        self._batch_ocr_task = task
        self._batch_ocr_root = self.root
        task.signals.progress.connect(self._on_batch_ocr_progress)
        task.signals.finished.connect(
            lambda result: self._on_batch_ocr_finished(task, result)
        )
        task.signals.error.connect(
            lambda message: self._on_batch_ocr_error(task, message)
        )
        self.btn_batch_ocr.setText("停止识别")
        self.btn_batch_ocr.setToolTip("识别在后台运行；再次点击可安全停止剩余任务")
        self._update_thread_pool.start(task)
        self.show_toast(
            f"已开始后台识别 {len(pending)} 个任务，照片采集和其他操作不受影响"
        )

    def _on_batch_ocr_progress(self, current: int, total: int, label: str):
        if self._batch_ocr_task is None:
            return
        self.btn_batch_ocr.setText(f"识别 {current}/{max(total, 1)}")
        self.btn_batch_ocr.setToolTip(label + "；点击可停止剩余任务")

    def _reset_batch_ocr_button(self):
        self.btn_batch_ocr.setEnabled(True)
        self.btn_batch_ocr.setText("开始识别")
        self.btn_batch_ocr.setToolTip(
            "照片收集完成后，后台识别半关门/全关门照片中的箱号、箱型和载重"
        )

    def _on_batch_ocr_finished(self, task: BatchOcrRunner, result: dict):
        if self._batch_ocr_task is not task:
            return
        self._batch_ocr_task = None
        self._batch_ocr_root = None
        self._reset_batch_ocr_button()
        self.refresh()
        message = (
            f"识别完成：{result.get('success_count', 0)} 个任务，"
            f"读取 {result.get('image_count', 0)} 张关门照片"
        )
        if result.get("error_count"):
            message += f"，失败 {result['error_count']} 个"
        if result.get("cancelled"):
            message = "已停止；" + message
        self.show_toast(message)

    def _on_batch_ocr_error(self, task: BatchOcrRunner, message: str):
        if self._batch_ocr_task is not task:
            return
        self._batch_ocr_task = None
        self._batch_ocr_root = None
        self._reset_batch_ocr_button()
        show_operation_error(
            self,
            "批量识别失败",
            "照片和现有 OCR 结果保持不变；可以稍后重新点击“开始识别”。",
            str(message),
        )

    def set_auto_ocr_enabled(self, enabled: bool):
        enabled = bool(enabled)
        previous = bool(self.conf.get("auto_ocr_enabled", True))
        self.conf["auto_ocr_enabled"] = enabled
        if not self.save_conf():
            self.conf["auto_ocr_enabled"] = previous
            self.chk_auto_ocr.blockSignals(True)
            self.chk_auto_ocr.setChecked(previous)
            self.chk_auto_ocr.blockSignals(False)
            if hasattr(self, "toast"):
                self.show_toast("无法保存自动 OCR 设置")
            return

        detail = getattr(self, "_detail_page", None)
        if detail is not None:
            if not enabled:
                detail._pending_auto_ocr = None
            detail.refresh_header_state()

        if hasattr(self, "task_data"):
            self.refresh()

        if enabled and detail is not None:
            for idx in (5, 4):
                images = detail.meta.get(str(idx), [])
                if images:
                    detail.maybe_auto_ocr(idx, images)
                    break

        if hasattr(self, "toast"):
            message = "自动 OCR 已开启" if enabled else "自动 OCR 已关闭，手动识别仍可使用"
            self.show_toast(message)

    def setup_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        main_layout = QHBoxLayout(cw)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # === Sidebar (可折叠) ===
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(232)
        self.sidebar.setStyleSheet(f"background-color: {Theme.BG_SIDEBAR}; border-right: 1px solid {Theme.BORDER};")
        sb_layout = QVBoxLayout(self.sidebar)
        sb_layout.setContentsMargins(10, 12, 10, 12)
        sb_layout.setSpacing(8)

        # Collapse toggle button
        self.btn_collapse = QPushButton()
        self.btn_collapse.setIcon(app_icon(self.btn_collapse, "collapse", Theme.TEXT_SEC))
        self.btn_collapse.setFixedSize(30, 30)
        self.btn_collapse.setCursor(Qt.PointingHandCursor)
        self.btn_collapse.setToolTip("折叠/展开侧边栏")
        self.btn_collapse.setStyleSheet(f"""
            QPushButton {{
                border: 1px solid transparent; border-radius: 8px;
                background: transparent; color: {Theme.TEXT_SEC};
                font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {Theme.BG_ACTIVE}; color: {Theme.TEXT_MAIN}; border-color: transparent; }}
        """)
        self.btn_collapse.clicked.connect(self.toggle_sidebar)
        collapse_row = QHBoxLayout()
        collapse_row.addStretch()
        collapse_row.addWidget(self.btn_collapse)
        sb_layout.addLayout(collapse_row)

        # App Brand
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 2, 0, 2)
        brand_row.setSpacing(8)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(30, 30)
        self.logo_label.setAlignment(Qt.AlignCenter)
        logo = QPixmap(str(resource_path(APP_LOGO_PNG)))
        if not logo.isNull():
            self.logo_label.setPixmap(logo.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand_row.addWidget(self.logo_label)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        self.app_title = QLabel(APP_DISPLAY_NAME)
        self.app_title.setStyleSheet(
            f"font-family: {Theme.FONT_FAMILY_BRAND}; font-size: 15px; "
            f"font-weight: 700; color: {Theme.TEXT_MAIN};"
        )
        self.app_subtitle = QLabel("照片采集 · OCR · 导出")
        self.app_subtitle.setStyleSheet(f"font-size: 12px; color: {Theme.TEXT_MUTED};")
        brand_text.addWidget(self.app_title)
        brand_text.addWidget(self.app_subtitle)
        brand_row.addLayout(brand_text, 1)
        sb_layout.addLayout(brand_row)

        self.lbl_views_section = QLabel("视图")
        self.lbl_views_section.setStyleSheet(f"font-weight:600; color:{Theme.TEXT_MUTED}; margin-top:12px;")
        sb_layout.addWidget(self.lbl_views_section)

        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        self.btn_tasks = QPushButton("任务工作台")
        self.btn_tasks.setCheckable(True)
        self.btn_tasks.setChecked(True)
        style_button(self.btn_tasks, "Ghost", "image")
        style_sidebar_button(self.btn_tasks)
        self.btn_tasks.clicked.connect(self.show_dashboard)
        view_group.addButton(self.btn_tasks)
        sb_layout.addWidget(self.btn_tasks)

        self.btn_report = QPushButton("采集看板")
        self.btn_report.setCheckable(True)
        style_button(self.btn_report, "Ghost", "chart")
        style_sidebar_button(self.btn_report)
        self.btn_report.clicked.connect(self.show_report)
        view_group.addButton(self.btn_report)
        sb_layout.addWidget(self.btn_report)

        # Directory Section
        self.lbl_dir_section = QLabel("工作目录")
        self.lbl_dir_section.setStyleSheet(f"font-weight:600; color:{Theme.TEXT_MUTED}; margin-top:12px;")
        sb_layout.addWidget(self.lbl_dir_section)

        self.cb_hist = QComboBox()
        self.cb_hist.setStyleSheet(f"""
            QComboBox {{ padding: 7px; border: 1px solid {Theme.BORDER}; border-radius: 7px; background:{Theme.BG_CARD}; color:{Theme.TEXT_MAIN}; }}
            QComboBox:focus {{ border-color: {Theme.PRIMARY}; }}
            QComboBox::drop-down {{ border: none; }}
        """)
        self.update_hist()
        self.cb_hist.activated.connect(self.change_hist)
        sb_layout.addWidget(self.cb_hist)

        self.btn_dir = QPushButton("切换目录")
        style_button(self.btn_dir, "Ghost", "folder")
        style_sidebar_button(self.btn_dir)
        self.btn_dir.clicked.connect(self.choose_dir)
        sb_layout.addWidget(self.btn_dir)

        # Actions Section
        self.lbl_actions_section = QLabel("工具")
        self.lbl_actions_section.setStyleSheet(f"font-weight:600; color:{Theme.TEXT_MUTED}; margin-top:12px;")
        sb_layout.addWidget(self.lbl_actions_section)

        self.ocr_toggle_row = QFrame()
        self.ocr_toggle_row.setToolTip("自动识别第 4、5 类关门照片；关闭后仍可手动识别")
        self.ocr_toggle_row.setStyleSheet(f"""
            QFrame {{ background: transparent; border: none; border-radius: 7px; }}
            QFrame:hover {{ background: {Theme.BG_ACTIVE}; }}
            QLabel {{ color: {Theme.TEXT_MAIN}; background: transparent; border: none; }}
        """)
        ocr_toggle_layout = QHBoxLayout(self.ocr_toggle_row)
        ocr_toggle_layout.setContentsMargins(9, 5, 7, 5)
        ocr_toggle_layout.setSpacing(8)
        ocr_toggle_icon = QLabel()
        ocr_toggle_icon.setFixedSize(18, 18)
        ocr_toggle_icon.setPixmap(app_icon(self, "search", Theme.TEXT_SEC).pixmap(16, 16))
        ocr_toggle_layout.addWidget(ocr_toggle_icon)
        ocr_toggle_layout.addWidget(QLabel("自动 OCR"))
        ocr_toggle_layout.addStretch()
        self.chk_auto_ocr = ToggleSwitch()
        self.chk_auto_ocr.setAccessibleName("自动 OCR")
        self.chk_auto_ocr.setToolTip(self.ocr_toggle_row.toolTip())
        self.chk_auto_ocr.setChecked(bool(self.conf["auto_ocr_enabled"]))
        self.chk_auto_ocr.toggled.connect(self.set_auto_ocr_enabled)
        ocr_toggle_layout.addWidget(self.chk_auto_ocr)
        sb_layout.addWidget(self.ocr_toggle_row)

        self.btn_batch_ocr = QPushButton("开始识别")
        style_button(self.btn_batch_ocr, "Ghost", "search")
        style_sidebar_button(self.btn_batch_ocr)
        self.btn_batch_ocr.setToolTip(
            "照片收集完成后，后台识别半关门/全关门照片中的箱号、箱型和载重"
        )
        self.btn_batch_ocr.clicked.connect(self.start_batch_ocr)
        sb_layout.addWidget(self.btn_batch_ocr)

        self.btn_req = QPushButton("设置验收标准")
        style_button(self.btn_req, "Ghost", "settings")
        style_sidebar_button(self.btn_req)
        self.btn_req.clicked.connect(self.open_reqs)
        sb_layout.addWidget(self.btn_req)

        self.btn_recent = QPushButton("最近删除")
        style_button(self.btn_recent, "Ghost", "delete")
        style_sidebar_button(self.btn_recent)
        self.btn_recent.clicked.connect(self.open_recent_deleted)
        sb_layout.addWidget(self.btn_recent)

        self.btn_health = QPushButton("数据检查")
        style_button(self.btn_health, "Ghost", "ok")
        style_sidebar_button(self.btn_health)
        self.btn_health.clicked.connect(self.open_data_health)
        sb_layout.addWidget(self.btn_health)

        self.btn_theme = QPushButton("界面配色")
        style_button(self.btn_theme, "Ghost", "settings")
        style_sidebar_button(self.btn_theme)
        self.btn_theme.clicked.connect(self.open_theme_choices)
        sb_layout.addWidget(self.btn_theme)

        self.btn_check_update = QPushButton("检查更新")
        style_button(self.btn_check_update, "Ghost", "refresh")
        style_sidebar_button(self.btn_check_update)
        self.btn_check_update.setToolTip(
            "后台检查群晖上的新版本；发现更新后可在软件内下载并重启安装"
        )
        self.btn_check_update.clicked.connect(
            lambda _checked=False: self.check_for_updates(manual=True)
        )
        sb_layout.addWidget(self.btn_check_update)

        sb_layout.addStretch()

        self.btn_about = QPushButton("关于")
        style_button(self.btn_about, "Ghost", "about")
        style_sidebar_button(self.btn_about)
        self.btn_about.clicked.connect(self.show_about)
        sb_layout.addWidget(self.btn_about)

        # 存储需要在折叠时隐藏的控件
        self._sidebar_widgets = [
            self.app_title, self.app_subtitle, self.lbl_views_section,
            self.lbl_dir_section, self.cb_hist, self.lbl_actions_section,
            self.ocr_toggle_row,
        ]
        # 按钮折叠时显示图标版
        self._sidebar_buttons = {
            self.btn_tasks: ("任务工作台", ""),
            self.btn_report: ("采集看板", ""),
            self.btn_batch_ocr: ("开始识别", ""),
            self.btn_req: ("设置验收标准", ""),
            self.btn_recent: ("最近删除", ""),
            self.btn_health: ("数据检查", ""),
            self.btn_theme: ("界面配色", ""),
            self.btn_check_update: ("检查更新", ""),
            self.btn_dir: ("切换目录", ""),
            self.btn_about: ("关于", ""),
        }

        main_layout.addWidget(self.sidebar)

        # === Main Content ===
        right_shell = QWidget()
        right_layout = QVBoxLayout(right_shell)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.topbar = QFrame()
        self.topbar.setFixedHeight(38)
        self.topbar.setStyleSheet(f"background:{Theme.BG_MAIN}; border-bottom:1px solid {Theme.BORDER};")
        self.topbar.installEventFilter(self)
        topbar_layout = QHBoxLayout(self.topbar)
        topbar_layout.setContentsMargins(10, 4, 10, 4)
        topbar_layout.setSpacing(4)
        topbar_layout.addStretch()
        self.btn_minimize = window_button(self, "minimize", "最小化")
        self.btn_maximize = window_button(self, "maximize", "最大化")
        self.btn_close = window_button(self, "close", "关闭", danger=True)
        self.btn_minimize.clicked.connect(self.showMinimized)
        self.btn_maximize.clicked.connect(self.toggle_maximized)
        self.btn_close.clicked.connect(self.close)
        topbar_layout.addWidget(self.btn_minimize)
        topbar_layout.addWidget(self.btn_maximize)
        topbar_layout.addWidget(self.btn_close)
        right_layout.addWidget(self.topbar)

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.dashboard_page = QWidget()
        content_layout = QVBoxLayout(self.dashboard_page)
        content_layout.setContentsMargins(20, 18, 20, 18)
        content_layout.setSpacing(12)

        self.dashboard_summary = QFrame()
        self.dashboard_summary.setObjectName("dashboardSummary")
        self.dashboard_summary.setStyleSheet(
            "QFrame#dashboardSummary { background:transparent; border:none; }"
        )
        self.dashboard_summary.setMinimumHeight(58)
        self.dashboard_summary_layout = QGridLayout(self.dashboard_summary)
        self.dashboard_summary_layout.setContentsMargins(0, 4, 0, 4)
        self.dashboard_summary_layout.setHorizontalSpacing(20)
        self.dashboard_summary_layout.setVerticalSpacing(8)

        self.dashboard_title_group = QWidget()
        title_box = QVBoxLayout(self.dashboard_title_group)
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        self.lbl_page_title = QLabel("任务工作台")
        self.lbl_page_title.setStyleSheet(f"font-size:16px; font-weight:800; color:{Theme.TEXT_MAIN};")
        self.lbl_page_subtitle = ElidedLabel(
            "选择工作目录后开始采集箱号照片",
            auto_tooltip=False,
        )
        self.lbl_page_subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        title_box.addWidget(self.lbl_page_title)
        title_box.addWidget(self.lbl_page_subtitle)

        def mk_stat(val_widget, label, color=Theme.TEXT_MAIN):
            group = QWidget()
            v = QVBoxLayout(group)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            val_widget.setStyleSheet(f"font-size:20px; font-weight:800; color:{color};")
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{Theme.TEXT_SEC}; font-size:11px;")
            v.addWidget(val_widget, 0, Qt.AlignLeft)
            v.addWidget(lbl, 0, Qt.AlignLeft)
            return group

        self.lbl_tot = QLabel("0")
        self.lbl_ok = QLabel("0")
        self.lbl_bad = QLabel("0")
        self.dashboard_stats_group = QWidget()
        stats_layout = QHBoxLayout(self.dashboard_stats_group)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(20)
        stats_layout.addWidget(mk_stat(self.lbl_tot, "总任务"))
        stats_layout.addWidget(mk_stat(self.lbl_ok, "验收完成", Theme.SUCCESS))
        stats_layout.addWidget(mk_stat(self.lbl_bad, "待处理", Theme.WARNING))
        self._dashboard_summary_compact = None
        self._apply_dashboard_summary_layout()
        content_layout.addWidget(self.dashboard_summary)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        content_layout.addWidget(self.progress)

        self.directory_alert = QFrame()
        self.directory_alert.setObjectName("directoryAlert")
        self.directory_alert.setStyleSheet(f"""
            QFrame#directoryAlert {{
                background: {Theme.WARNING_SOFT};
                border: 1px solid {Theme.WARNING_LINE};
                border-radius: 8px;
            }}
            QFrame#directoryAlert QLabel {{
                background: transparent;
                border: none;
            }}
        """)
        directory_alert_layout = QVBoxLayout(self.directory_alert)
        directory_alert_layout.setContentsMargins(12, 10, 12, 10)
        directory_alert_layout.setSpacing(8)

        directory_alert_message = QHBoxLayout()
        directory_alert_message.setSpacing(9)
        directory_alert_icon = QLabel()
        directory_alert_icon.setFixedSize(20, 20)
        directory_alert_icon.setPixmap(app_icon(self, "warn", Theme.WARNING).pixmap(18, 18))
        directory_alert_message.addWidget(directory_alert_icon, 0, Qt.AlignTop)

        directory_alert_text = QVBoxLayout()
        directory_alert_text.setSpacing(2)
        self.directory_alert_title = QLabel("上次工作目录不可用")
        self.directory_alert_title.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{Theme.TEXT_MAIN};"
        )
        self.directory_alert_path = QLabel()
        self.directory_alert_path.setWordWrap(True)
        self.directory_alert_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.directory_alert_path.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_SEC};")
        directory_alert_text.addWidget(self.directory_alert_title)
        directory_alert_text.addWidget(self.directory_alert_path)
        directory_alert_message.addLayout(directory_alert_text, 1)
        directory_alert_layout.addLayout(directory_alert_message)

        directory_alert_actions = QHBoxLayout()
        directory_alert_actions.setContentsMargins(29, 0, 0, 0)
        directory_alert_actions.setSpacing(8)
        directory_alert_actions.addStretch()
        self.btn_directory_history = QToolButton()
        self.btn_directory_history.setText("历史目录")
        self.btn_directory_history.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_directory_history.setIcon(app_icon(self.btn_directory_history, "folder", Theme.TEXT_SEC))
        self.btn_directory_history.setPopupMode(QToolButton.InstantPopup)
        self.directory_history_menu = styled_menu(self.btn_directory_history)
        self.directory_history_menu.aboutToShow.connect(self._rebuild_directory_history_menu)
        self.btn_directory_history.setMenu(self.directory_history_menu)
        style_command_button(self.btn_directory_history, text_button=True)
        self.btn_directory_history.setFixedHeight(34)
        directory_alert_actions.addWidget(self.btn_directory_history)
        self.btn_reselect_directory = QPushButton("重新选择目录")
        style_button(self.btn_reselect_directory, "Primary", "folder")
        self.btn_reselect_directory.setFixedHeight(34)
        self.btn_reselect_directory.clicked.connect(self.choose_dir)
        directory_alert_actions.addWidget(self.btn_reselect_directory)
        directory_alert_layout.addLayout(directory_alert_actions)
        self.directory_alert.hide()
        content_layout.addWidget(self.directory_alert)

        self.command_bar = QFrame()
        command_layout = QVBoxLayout(self.command_bar)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(8)

        self.primary_commands = QGridLayout()
        self.primary_commands.setContentsMargins(0, 0, 0, 0)
        self.primary_commands.setHorizontalSpacing(8)
        self.primary_commands.setVerticalSpacing(6)

        self.command_actions = QWidget()
        self.command_actions.setStyleSheet("background: transparent;")
        command_actions_layout = QHBoxLayout(self.command_actions)
        command_actions_layout.setContentsMargins(0, 0, 0, 0)
        command_actions_layout.setSpacing(8)

        self.btn_new = QPushButton("批量新建")
        style_button(self.btn_new, "Primary", "add")
        self.btn_new.setFixedHeight(34)
        self.btn_new.clicked.connect(self.open_batch)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索箱号、箱型或OCR结果")
        self.search.setFixedHeight(34)
        self.search.setMaximumWidth(360)
        self.search.setMinimumWidth(180)
        self.search.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Debounce keystrokes so the task list is rebuilt once per pause,
        # not once per typed character. All other callers still hit
        # render_list() directly and stay synchronous.
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(120)
        self._search_debounce.timeout.connect(
            lambda: self.render_list(self.search.text())
        )
        self.search.textChanged.connect(
            lambda _text: self._search_debounce.start()
        )

        self.btn_sort = QToolButton()
        self.btn_sort.setIcon(app_icon(self.btn_sort, "sort", Theme.TEXT_SEC))
        sort_labels = {"status": "状态优先", "name": "按箱号", "updated": "最近更新"}
        self.btn_sort.setToolTip(f"排序：{sort_labels[self._sort_order]}")
        self.btn_sort.setFixedSize(34, 34)
        self.btn_sort.setPopupMode(QToolButton.InstantPopup)
        style_command_button(self.btn_sort)
        sort_menu = styled_menu(self.btn_sort)
        self.sort_actions = {}
        for key, label in (("status", "状态优先"), ("name", "按箱号"), ("updated", "最近更新")):
            action = sort_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self._sort_order)
            action.triggered.connect(lambda checked=False, value=key: self.set_sort(value))
            self.sort_actions[key] = action
        self.btn_sort.setMenu(sort_menu)
        command_actions_layout.addWidget(self.btn_sort)

        self.btn_refresh = QToolButton()
        self.btn_refresh.setIcon(app_icon(self.btn_refresh, "refresh", Theme.TEXT_SEC))
        self.btn_refresh.setToolTip("刷新")
        self.btn_refresh.setFixedSize(34, 34)
        style_command_button(self.btn_refresh)
        self.btn_refresh.clicked.connect(self.refresh)
        command_actions_layout.addWidget(self.btn_refresh)

        self.btn_export = QToolButton()
        self.btn_export.setText("导出资料")
        self.btn_export.setIcon(app_icon(self.btn_export, "export", Theme.TEXT_SEC))
        self.btn_export.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_export.setPopupMode(QToolButton.InstantPopup)
        self.btn_export.setFixedHeight(34)
        self.btn_export.setMinimumWidth(92)
        style_command_button(self.btn_export, text_button=True)
        export_menu = styled_menu(self.btn_export)
        export_all = export_menu.addAction("照片交接包 ZIP · 全部任务")
        export_all.setIcon(app_icon(self, "export"))
        export_complete = export_menu.addAction("照片交接包 ZIP · 仅已完成")
        export_complete.setIcon(app_icon(self, "export"))
        export_all.triggered.connect(lambda: self.export_handoff("all"))
        export_complete.triggered.connect(lambda: self.export_handoff("complete"))
        export_selected = export_menu.addAction("照片交接包 ZIP · 所选任务")
        export_selected.setIcon(app_icon(self, "export"))
        export_selected.triggered.connect(lambda: self.export_handoff("selected"))
        export_menu.addSeparator()
        export_excel = export_menu.addAction("采集看板 Excel")
        export_excel.setIcon(app_icon(self, "chart"))
        export_excel.triggered.connect(lambda: self.export_collection_excel(False))
        export_selected_excel = export_menu.addAction("采集看板 Excel · 所选任务")
        export_selected_excel.setIcon(app_icon(self, "chart"))
        export_selected_excel.triggered.connect(
            lambda: self.export_collection_excel(True)
        )
        export_menu.addSeparator()
        export_naming = export_menu.addAction("设置导出文件名")
        export_naming.setIcon(app_icon(self, "settings"))
        export_naming.triggered.connect(self.open_export_naming)
        self.btn_export.setMenu(export_menu)
        command_actions_layout.addWidget(self.btn_export)
        command_layout.addLayout(self.primary_commands)
        self._command_bar_compact = None
        self._apply_command_bar_layout()

        filter_strip = QFrame()
        filter_strip.setObjectName("filterStrip")
        filter_strip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        filter_strip.setStyleSheet(f"QFrame#filterStrip {{ background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; border-radius:8px; }}")
        filter_layout = QHBoxLayout(filter_strip)
        filter_layout.setContentsMargins(3, 3, 3, 3)
        filter_layout.setSpacing(2)
        self.filter_buttons = {}
        filter_group = QButtonGroup(self)
        filter_group.setExclusive(True)
        for key, label in (("all", "全部"), ("pending", "待补"), ("anomaly", "核验"), ("complete", "完成")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("filterLabel", label)
            button.setFixedHeight(28)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(f"""
                QPushButton {{
                    border:1px solid transparent;
                    border-radius:6px;
                    padding:3px 10px;
                    background:transparent;
                    color:{Theme.TEXT_SEC};
                    font-size:12px;
                }}
                QPushButton:hover {{ background:{Theme.BG_ACTIVE}; color:{Theme.TEXT_MAIN}; }}
                QPushButton:focus {{ border-color:{Theme.PRIMARY}; }}
                QPushButton:checked {{ background:{Theme.BG_CARD}; color:{Theme.TEXT_MAIN}; border-color:{Theme.BORDER}; font-weight:700; }}
            """)
            button.clicked.connect(lambda checked=False, value=key: self.set_filter(value))
            if key == "anomaly":
                button.setToolTip("待核验、未识别或箱号异常")
            filter_group.addButton(button)
            self.filter_buttons[key] = button
            filter_layout.addWidget(button)
        self.filter_buttons[self._current_filter].setChecked(True)
        filter_layout.addStretch()
        command_layout.addWidget(filter_strip)
        content_layout.addWidget(self.command_bar)

        list_container = QFrame()
        list_container.setProperty("class", "Card")
        lc_layout = QVBoxLayout(list_container)
        lc_layout.setContentsMargins(0, 10, 0, 10)
        lh = QHBoxLayout()
        lh.setContentsMargins(20, 0, 20, 8)
        lh.addWidget(QLabel("任务列表", styleSheet=f"font-weight:700; font-size:14px; color:{Theme.TEXT_MAIN};"))
        self.lbl_list_count = QLabel("0 项")
        self.lbl_list_count.setStyleSheet(f"color:{Theme.TEXT_MUTED}; font-size:12px;")
        lh.addWidget(self.lbl_list_count)
        lh.addStretch()
        self.lbl_selection_count = QLabel()
        self.lbl_selection_count.setStyleSheet(
            f"color:{Theme.PRIMARY}; font-size:12px; font-weight:700;"
        )
        lh.addWidget(self.lbl_selection_count)

        self.btn_selection_export = QToolButton()
        self.btn_selection_export.setText("导出所选")
        self.btn_selection_export.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_selection_export.setIcon(
            app_icon(self.btn_selection_export, "export", Theme.TEXT_SEC)
        )
        self.btn_selection_export.setPopupMode(QToolButton.InstantPopup)
        selection_export_menu = styled_menu(self.btn_selection_export)
        selected_zip = selection_export_menu.addAction("照片交接包 ZIP")
        selected_zip.triggered.connect(lambda: self.export_handoff("selected"))
        selected_excel = selection_export_menu.addAction("采集看板 Excel")
        selected_excel.triggered.connect(
            lambda: self.export_collection_excel(True)
        )
        self.btn_selection_export.setMenu(selection_export_menu)
        style_command_button(self.btn_selection_export, text_button=True)
        lh.addWidget(self.btn_selection_export)

        self.btn_selection_reqs = QPushButton("统一标准")
        style_button(self.btn_selection_reqs, "Ghost", "settings")
        self.btn_selection_reqs.clicked.connect(self.open_batch_requirements)
        lh.addWidget(self.btn_selection_reqs)

        self.btn_selection_delete = QPushButton("删除所选")
        style_button(self.btn_selection_delete, "Danger", "delete")
        self.btn_selection_delete.clicked.connect(self._delete_selected_task)
        lh.addWidget(self.btn_selection_delete)
        lc_layout.addLayout(lh)

        self.list_stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setAlternatingRowColors(False)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # All rows share one 64px size hint; telling Qt so skips per-row
        # measuring and keeps large lists snappy. Pixel scrolling feels
        # smoother than whole-row jumps on long lists.
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setStyleSheet(f"""
            QListWidget {{ background:transparent; padding:8px; }}
            QListWidget::item {{ border-radius:8px; margin:1px 0; }}
            QListWidget::item:hover {{ background-color:{Theme.BG_SUBTLE}; }}
            QListWidget::item:selected {{ background-color:{Theme.BG_ACTIVE}; color:{Theme.TEXT_MAIN}; }}
        """)
        self.list.itemDoubleClicked.connect(self.open_box)
        self.list.itemSelectionChanged.connect(self._update_selection_actions)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_ctx_menu)
        self.list.verticalScrollBar().valueChanged.connect(
            self._materialize_visible_task_rows
        )
        self.list_stack.addWidget(self.list)
        self._update_selection_actions()

        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setSpacing(8)
        empty_layout.addStretch()
        self.empty_icon = QLabel()
        self.empty_icon.setFixedSize(56, 56)
        self.empty_icon.setAlignment(Qt.AlignCenter)
        self.empty_icon.setPixmap(app_icon(self, "image", Theme.TEXT_MUTED).pixmap(26, 26))
        self.empty_icon.setStyleSheet(
            f"background:{Theme.BG_SUBTLE}; border:1px solid {Theme.BORDER}; border-radius:28px;"
        )
        empty_layout.addWidget(self.empty_icon, 0, Qt.AlignCenter)
        self.empty_title = QLabel("还没有任务")
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_title.setStyleSheet(f"font-size:16px; font-weight:700; color:{Theme.TEXT_MAIN};")
        empty_layout.addWidget(self.empty_title)
        self.empty_detail = QLabel("批量创建箱号后即可开始采集照片")
        self.empty_detail.setAlignment(Qt.AlignCenter)
        self.empty_detail.setWordWrap(True)
        self.empty_detail.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        empty_layout.addWidget(self.empty_detail)
        self.empty_action = QPushButton("批量新建")
        style_button(self.empty_action, "Primary", "add")
        self.empty_action.clicked.connect(self._handle_empty_action)
        empty_layout.addWidget(self.empty_action, 0, Qt.AlignCenter)
        empty_layout.addStretch()
        self.list_stack.addWidget(empty_page)
        lc_layout.addWidget(self.list_stack)
        content_layout.addWidget(list_container)

        self.shortcut_search = QShortcut(QKeySequence.Find, self)
        self.shortcut_search.activated.connect(self.search.setFocus)
        self.shortcut_refresh = QShortcut(QKeySequence.Refresh, self)
        self.shortcut_refresh.activated.connect(self.refresh)
        self.shortcut_open = QShortcut(QKeySequence("Return"), self.list)
        self.shortcut_open.activated.connect(self.open_selected_task)
        self.shortcut_new = QShortcut(QKeySequence.New, self)
        self.shortcut_new.activated.connect(self.open_batch)
        self.shortcut_delete = QShortcut(QKeySequence.Delete, self.list)
        self.shortcut_delete.setContext(Qt.WidgetShortcut)
        self.shortcut_delete.activated.connect(self._delete_selected_task)
        self.setTabOrder(self.btn_new, self.search)
        self.setTabOrder(self.search, self.btn_sort)
        self.setTabOrder(self.btn_sort, self.btn_refresh)
        self.setTabOrder(self.btn_refresh, self.btn_export)
        previous = self.btn_export
        for key in ("all", "pending", "anomaly", "complete"):
            button = self.filter_buttons[key]
            self.setTabOrder(previous, button)
            previous = button
        self.setTabOrder(previous, self.list)
        
        self.stack.addWidget(self.dashboard_page)
        self.report_page = CollectionDashboardPage(self)
        self.report_page.refresh_requested.connect(self.refresh)
        self.report_page.export_requested.connect(self.export_collection_excel)
        self.report_page.task_open_requested.connect(self.open_box_name)
        self.stack.addWidget(self.report_page)
        self._detail_page = None
        right_layout.addWidget(self.stack)
        main_layout.addWidget(right_shell, 1)
        self._apply_sidebar_state()

    def _apply_dashboard_summary_layout(self):
        if not hasattr(self, "dashboard_summary_layout"):
            return
        compact = self.width() < 720
        if compact == getattr(self, "_dashboard_summary_compact", None):
            return

        layout = self.dashboard_summary_layout
        layout.removeWidget(self.dashboard_title_group)
        layout.removeWidget(self.dashboard_stats_group)
        for column in range(2):
            layout.setColumnStretch(column, 0)
        if compact:
            layout.addWidget(self.dashboard_title_group, 0, 0)
            layout.addWidget(self.dashboard_stats_group, 1, 0)
            layout.setColumnStretch(0, 1)
        else:
            layout.addWidget(self.dashboard_title_group, 0, 0)
            layout.addWidget(self.dashboard_stats_group, 0, 1, Qt.AlignRight)
            layout.setColumnStretch(0, 1)
        self._dashboard_summary_compact = compact

    def _apply_command_bar_layout(self):
        if not hasattr(self, "primary_commands"):
            return
        compact = self.width() < 560
        if compact == getattr(self, "_command_bar_compact", None):
            return

        layout = self.primary_commands
        for widget in (self.btn_new, self.search, self.command_actions):
            layout.removeWidget(widget)
        for column in range(4):
            layout.setColumnStretch(column, 0)

        if compact:
            self.search.setMinimumWidth(0)
            self.search.setMaximumWidth(16777215)
            layout.addWidget(self.btn_new, 0, 0, 1, 1, Qt.AlignLeft)
            layout.addWidget(self.search, 0, 1)
            layout.addWidget(self.command_actions, 1, 0, 1, 2, Qt.AlignRight)
            layout.setColumnStretch(1, 1)
        else:
            self.search.setMinimumWidth(180)
            self.search.setMaximumWidth(360)
            layout.addWidget(self.btn_new, 0, 0, 1, 1, Qt.AlignLeft)
            layout.addWidget(self.search, 0, 2)
            layout.addWidget(self.command_actions, 0, 3, Qt.AlignRight)
            layout.setColumnStretch(1, 1)
        self._command_bar_compact = compact

    def _enable_windows_snap(self) -> bool:
        if sys.platform != "win32" or QApplication.platformName().lower() != "windows":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winId())
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_style = user32.GetWindowLongPtrW
            get_style.argtypes = (wintypes.HWND, ctypes.c_int)
            get_style.restype = ctypes.c_ssize_t
            set_style = user32.SetWindowLongPtrW
            set_style.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            set_style.restype = ctypes.c_ssize_t

            current_style = int(get_style(hwnd, GWL_STYLE))
            required_style = current_style | WINDOWS_SNAP_STYLE
            if required_style != current_style:
                ctypes.set_last_error(0)
                previous_style = set_style(hwnd, GWL_STYLE, required_style)
                if previous_style == 0 and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                frame_flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
                if not user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, frame_flags):
                    raise ctypes.WinError(ctypes.get_last_error())
            self._windows_snap_initialized = True
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            log.exception("Unable to enable native Windows snap support")
            self._windows_snap_initialized = False
            return False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._windows_snap_initialized:
            self._enable_windows_snap()

    def _windows_hit_test(self, global_pos: QPoint):
        if hasattr(self, "btn_maximize") and self.btn_maximize.isVisible():
            button_origin = self.btn_maximize.mapToGlobal(QPoint(0, 0))
            button_rect = QRect(button_origin, self.btn_maximize.size())
            if button_rect.contains(global_pos):
                return HTMAXBUTTON

        for button_name in ("btn_minimize", "btn_close"):
            button = getattr(self, button_name, None)
            if button is None or not button.isVisible():
                continue
            button_origin = button.mapToGlobal(QPoint(0, 0))
            if QRect(button_origin, button.size()).contains(global_pos):
                return None

        if not self.isMaximized() and not self.isFullScreen():
            pos = self.mapFromGlobal(global_pos)
            border = 7
            left = pos.x() < border
            right = pos.x() >= self.width() - border
            top = pos.y() < border
            bottom = pos.y() >= self.height() - border
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM

        topbar = getattr(self, "topbar", None)
        if topbar is not None and topbar.isVisible():
            topbar_origin = topbar.mapToGlobal(QPoint(0, 0))
            if QRect(topbar_origin, topbar.size()).contains(global_pos):
                return HTCAPTION
        return None

    def _handle_windows_maximize_mouse(self, message: int, wparam: int, global_pos: QPoint) -> bool:
        is_down = message == WM_LBUTTONDOWN or message == WM_NCLBUTTONDOWN
        is_up = message == WM_LBUTTONUP or message == WM_NCLBUTTONUP
        if not is_down and not is_up:
            return False

        if message == WM_NCLBUTTONDOWN or message == WM_NCLBUTTONUP:
            hit = int(wparam)
        else:
            hit = self._windows_hit_test(global_pos)

        if is_down:
            if hit != HTMAXBUTTON:
                return False
            self._native_maximize_pressed = True
            self.btn_maximize.setDown(True)
            return True

        if not self._native_maximize_pressed:
            return False
        self._native_maximize_pressed = False
        self.btn_maximize.setDown(False)
        if hit == HTMAXBUTTON:
            QTimer.singleShot(0, self.toggle_maximized)
        return True

    def _clear_titlebar_drag(self) -> None:
        self._drag_pos = None
        self._drag_press_global = None

    def _restore_from_maximized_drag(self, global_pos: QPoint, local_pos: QPoint) -> bool:
        press_pos = self._drag_press_global
        if press_pos is None:
            return False
        if (global_pos - press_pos).manhattanLength() < QApplication.startDragDistance():
            return False

        restore_geometry = self.normalGeometry()
        restore_width = restore_geometry.width()
        if restore_width <= 0:
            restore_width = max(self.minimumWidth(), min(1280, self.width()))

        ratio = max(0.0, min(1.0, self._drag_restore_ratio))
        cursor_offset_y = max(0, min(local_pos.y(), self.topbar.height() - 1))
        self.showNormal()
        self.resize(restore_geometry.size())

        screen = QApplication.screenAt(global_pos) or self.screen()
        available = screen.availableGeometry() if screen is not None else QRect()
        target_x = global_pos.x() - round(restore_width * ratio)
        target_y = global_pos.y() - cursor_offset_y
        if available.isValid():
            max_x = available.right() - self.width() + 1
            max_y = available.bottom() - self.topbar.height() + 1
            target_x = max(available.left(), min(target_x, max_x))
            target_y = max(available.top(), min(target_y, max_y))
        self.move(target_x, target_y)
        self.update_maximize_button()

        self._drag_pos = global_pos - self.frameGeometry().topLeft()
        self._drag_press_global = None
        handle = self.windowHandle()
        if handle is not None and handle.startSystemMove():
            self._drag_pos = None
        return True

    def eventFilter(self, obj, event):
        if obj is getattr(self, "topbar", None):
            if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._clear_titlebar_drag()
                self.toggle_maximized()
                return True
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                global_pos = event.globalPosition().toPoint()
                self._drag_press_global = global_pos
                self._drag_restore_ratio = (
                    event.position().x() / max(1, self.topbar.width())
                )
                self._drag_pos = global_pos - self.frameGeometry().topLeft()
                if self.isMaximized():
                    return True
                handle = self.windowHandle()
                if handle is not None and handle.startSystemMove():
                    self._drag_pos = None
                    return True
                return True
            if event.type() == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                global_pos = event.globalPosition().toPoint()
                if self.isMaximized():
                    self._restore_from_maximized_drag(
                        global_pos,
                        event.position().toPoint(),
                    )
                elif self._drag_pos is not None:
                    self.move(global_pos - self._drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._clear_titlebar_drag()
                return True
        return super().eventFilter(obj, event)

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32":
            try:
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if self._handle_windows_maximize_mouse(
                    int(msg.message), int(msg.wParam), QCursor.pos()
                ):
                    return True, 0
                if msg.message == WM_NCHITTEST:
                    hit = self._windows_hit_test(QCursor.pos())
                    if hit is not None:
                        return True, hit
            except (ValueError, OSError, TypeError, OverflowError):
                pass
        return super().nativeEvent(event_type, message)

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.update_maximize_button()

    def update_maximize_button(self):
        if self.isMaximized():
            self.btn_maximize.setIcon(app_icon(self, "restore", Theme.TEXT_SEC))
            self.btn_maximize.setToolTip("还原")
        else:
            self.btn_maximize.setIcon(app_icon(self, "maximize", Theme.TEXT_SEC))
            self.btn_maximize.setToolTip("最大化")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and hasattr(self, "btn_maximize"):
            self.update_maximize_button()

    def _start_update_notification_listener(self):
        if (
            self._update_notification_thread is not None
            and self._update_notification_thread.is_alive()
        ):
            return

        def emit_release(payload):
            try:
                self._update_notification_bridge.release_ready.emit(payload)
            except RuntimeError:
                pass

        self._update_notification_thread = threading.Thread(
            target=watch_update_notifications,
            args=(self._update_notification_stop, emit_release),
            name="PhotoCollectUpdateNotifications",
            daemon=True,
        )
        self._update_notification_thread.start()

    def _on_update_notification(self, payload: dict):
        if self._closing:
            return
        log.info(
            "Received release notification for v%s",
            payload.get("version", ""),
        )
        self.check_for_updates(manual=False)

    def _reset_update_check_button(self):
        if not hasattr(self, "btn_check_update"):
            return
        self.btn_check_update.setEnabled(True)
        self.btn_check_update.setText(
            "" if self._sidebar_collapsed else "检查更新"
        )
        self.btn_check_update.setToolTip(
            "后台检查群晖上的新版本；发现更新后可在软件内下载并重启安装"
        )

    def check_for_updates(self, manual: bool = False):
        if self._closing:
            return
        if self._update_check_task is not None:
            if manual:
                self.show_toast("正在后台检查更新，请稍候")
            return
        task = UpdateCheckTask(APP_VERSION, manual=manual)
        self._update_check_task = task
        task.signals.finished.connect(
            lambda result, current=task: self._on_update_check_finished(
                current,
                result,
                manual=manual,
            )
        )
        task.signals.error.connect(
            lambda message, current=task: self._on_update_check_error(
                current,
                message,
                manual=manual,
            )
        )
        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText(
            "" if self._sidebar_collapsed else "检查中..."
        )
        self.btn_check_update.setToolTip("正在后台连接更新服务器")
        self._update_thread_pool.start(task)

    def _on_update_check_finished(
        self,
        task: UpdateCheckTask,
        result: UpdateCheckResult,
        *,
        manual: bool,
    ):
        if self._update_check_task is not task:
            return
        self._update_check_task = None
        self._reset_update_check_button()
        if self._closing:
            return
        if not result.available:
            if manual:
                self.show_toast(f"已是最新版本 v{APP_VERSION}")
            return

        manifest = result.manifest
        self._available_update = manifest
        if (
            not manual
            and self._last_update_toast_release_id == manifest.release_id
        ):
            return
        self._last_update_toast_release_id = manifest.release_id
        self.show_toast(
            f"发现新版本 v{manifest.version}",
            "立即更新",
            lambda release=manifest: self.start_update_download(release),
            duration=30_000,
            secondary_text="更新说明",
            secondary_callback=lambda release=manifest: self.show_update_notes(
                release
            ),
        )

    def _on_update_check_error(
        self,
        task: UpdateCheckTask,
        message: str,
        *,
        manual: bool,
    ):
        if self._update_check_task is not task:
            return
        self._update_check_task = None
        self._reset_update_check_button()
        log.info("Unable to check for updates: %s", message)
        if manual and not self._closing:
            self.show_toast(f"暂时无法检查更新：{message}")

    @staticmethod
    def _format_update_size(size_bytes: int) -> str:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    def show_update_notes(self, manifest: UpdateManifest | None = None):
        release = manifest or self._available_update
        if release is None:
            return
        notes = release.notes.strip() or "本次更新未填写说明。"
        QMessageBox.information(
            self,
            f"{APP_DISPLAY_NAME} v{release.version}",
            (
                f"{notes}\n\n"
                f"文件大小：{self._format_update_size(release.size_bytes)}\n"
                f"发布时间：{release.published_at}"
            ),
        )

    def start_update_download(self, manifest: UpdateManifest | None = None):
        release = manifest or self._available_update
        if release is None:
            self.show_toast("请先检查更新")
            return
        if self._pending_install_plan is not None:
            self.restart_to_install_update()
            return
        if self._update_download_task is not None:
            self.show_toast("新版本正在后台下载")
            return
        if not getattr(sys, "frozen", False):
            self.show_toast(
                "当前是源码运行模式，只能检查更新；正式单文件版可自动安装"
            )
            return

        answer = QMessageBox.question(
            self,
            f"更新到 v{release.version}",
            (
                f"将在后台下载 {self._format_update_size(release.size_bytes)}，"
                "下载期间可以继续采集照片。\n\n"
                "下载完成后由你确认重启安装。现在开始吗？"
            ),
        )
        if answer != QMessageBox.Yes:
            return

        task = UpdateDownloadTask(release, Path(sys.executable))
        self._update_download_task = task
        task.signals.progress.connect(
            lambda current, total, active=task: self._on_update_download_progress(
                active,
                current,
                total,
            )
        )
        task.signals.finished.connect(
            lambda plan, active=task: self._on_update_download_finished(
                active,
                plan,
            )
        )
        task.signals.cancelled.connect(
            lambda active=task: self._on_update_download_cancelled(active)
        )
        task.signals.error.connect(
            lambda message, active=task: self._on_update_download_error(
                active,
                message,
            )
        )

        progress = QProgressDialog(
            f"正在后台下载 {APP_DISPLAY_NAME} v{release.version}",
            "取消下载",
            0,
            100,
            self,
        )
        progress.setWindowTitle("软件更新")
        progress.setWindowModality(Qt.NonModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.canceled.connect(task.cancel)
        progress.show()
        self._update_progress = progress
        QThreadPool.globalInstance().start(task)
        self.show_toast("新版本已在后台开始下载，其他操作不受影响")

    def _on_update_download_progress(
        self,
        task: UpdateDownloadTask,
        current: int,
        total: int,
    ):
        if self._update_download_task is not task:
            return
        progress = self._update_progress
        if progress is None:
            return
        percent = int(min(100, max(0, current * 100 / max(total, 1))))
        progress.setValue(percent)
        progress.setLabelText(
            f"正在后台下载新版本：{percent}%"
        )

    def _finish_update_progress(self):
        progress = self._update_progress
        self._update_progress = None
        if progress is not None:
            progress.close()
            progress.deleteLater()

    def _on_update_download_finished(
        self,
        task: UpdateDownloadTask,
        plan: InstallPlan,
    ):
        if self._update_download_task is not task:
            return
        self._update_download_task = None
        self._finish_update_progress()
        if self._closing:
            discard_install_plan(plan)
            return
        self._pending_install_plan = plan
        self.show_toast(
            "新版本已下载并校验完成，可重启安装",
            "重启更新",
            self.restart_to_install_update,
            duration=24 * 60 * 60 * 1000,
        )

    def _on_update_download_cancelled(self, task: UpdateDownloadTask):
        if self._update_download_task is not task:
            return
        self._update_download_task = None
        self._finish_update_progress()
        if not self._closing:
            self.show_toast("已取消更新下载，现有版本不受影响")

    def _on_update_download_error(
        self,
        task: UpdateDownloadTask,
        message: str,
    ):
        if self._update_download_task is not task:
            return
        self._update_download_task = None
        self._finish_update_progress()
        if not self._closing:
            show_operation_error(
                self,
                "更新下载失败",
                "现有版本没有被改动，可以稍后重新检查更新。",
                message,
            )

    def restart_to_install_update(self):
        plan = self._pending_install_plan
        if plan is None:
            self.show_toast("尚未下载可安装的新版本")
            return
        if self._export_task is not None or self._batch_ocr_task is not None:
            self.show_toast("请先等待导出或 OCR 任务结束，再重启更新")
            return
        answer = QMessageBox.question(
            self,
            "重启并安装更新",
            (
                "软件将关闭并自动替换为新版本，然后重新打开。\n"
                "若新版本启动失败，会自动恢复当前版本。是否继续？"
            ),
        )
        if answer != QMessageBox.Yes:
            return
        try:
            launch_install_helper(plan)
        except UpdateError as exc:
            show_operation_error(
                self,
                "无法启动更新助手",
                "现有版本没有被改动。",
                str(exc),
            )
            return
        self._installing_update = True
        self.close()

    def show_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"关于 {APP_DISPLAY_NAME}")
        dlg.setFixedSize(380, 270)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(12)
        logo_label = QLabel()
        logo_label.setFixedSize(46, 46)
        logo = QPixmap(str(resource_path(APP_LOGO_PNG)))
        if not logo.isNull():
            logo_label.setPixmap(logo.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo_row.addWidget(logo_label)

        title_box = QVBoxLayout()
        title = QLabel(APP_DISPLAY_NAME)
        title.setStyleSheet(
            f"font-family: {Theme.FONT_FAMILY_BRAND}; "
            f"font-size:19px; font-weight:800; color:{Theme.TEXT_MAIN};"
        )
        subtitle = QLabel("按箱号采集、校验和导出照片资料")
        subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        logo_row.addLayout(title_box, 1)
        layout.addLayout(logo_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{Theme.BORDER};")
        layout.addWidget(line)

        info = QLabel(f"作者：RenYiZheng\n版本：v{APP_VERSION}\n用途：按箱号采集、校验和导出照片资料。")
        info.setStyleSheet(f"font-size:13px; color:{Theme.TEXT_MAIN}; line-height:1.5;")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()

        buttons = QHBoxLayout()
        btn_logs = QPushButton("打开日志目录")
        style_button(btn_logs, icon_name="folder")
        btn_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR))))
        buttons.addWidget(btn_logs)
        buttons.addStretch()
        btn = QPushButton("关闭")
        style_button(btn)
        btn.clicked.connect(dlg.accept)
        buttons.addWidget(btn)
        layout.addLayout(buttons)
        dlg.exec()

    def log(self, m):
        # Note: never touch QMainWindow.statusBar() here — first access would
        # create a native status bar strip at the bottom of the frameless window.
        log.info("%s", m)

    def show_toast(
        self,
        text: str,
        action_text: str = "",
        callback=None,
        duration: int = 6000,
        secondary_text: str = "",
        secondary_callback=None,
    ):
        self.toast.show_message(
            text,
            action_text,
            callback,
            duration,
            secondary_text,
            secondary_callback,
        )

    def _discard_detail_page(self):
        if self._detail_page is None:
            return
        self._detail_page.prepare_for_disposal()
        self.stack.removeWidget(self._detail_page)
        self._detail_page.deleteLater()
        self._detail_page = None
        _thumb_cache.trim_to_fraction(0.25)

    def _on_background_import_committed(self, root, name, summary):
        if self.root == root and summary.added_names:
            self._mark_detail_dirty(name)
            if self._detail_page is None:
                QTimer.singleShot(0, self._apply_pending_task_updates)

    def show_dashboard(self, _checked=False):
        self._discard_detail_page()
        self.stack.setCurrentWidget(self.dashboard_page)
        self.btn_tasks.setChecked(True)
        if self._dirty_task_names:
            QTimer.singleShot(0, self._apply_pending_task_updates)

    def show_report(self, _checked=False):
        self._discard_detail_page()
        self._apply_pending_task_updates()
        self.stack.setCurrentWidget(self.report_page)
        self.btn_report.setChecked(True)
        if self._report_dirty:
            self._refresh_collection_report()

    def _refresh_collection_report(self):
        self.collection_report = build_collection_report(self.task_data, ALL_CATEGORIES)
        root_label = str(self.root) if self.root and self._work_root_state == "ready" else ""
        self.report_page.set_report(self.collection_report, root_label)
        if self._export_task is not None:
            self.report_page.btn_export.setEnabled(False)
        self._report_dirty = False

    def _set_work_root_state(self, state: str, detail: str = "") -> None:
        self._work_root_state = state
        if not hasattr(self, "directory_alert"):
            return

        if state in {"checking", "unavailable"}:
            path_text = str(self.root) if self.root else ""
            title = "正在连接工作目录" if state == "checking" else "上次工作目录不可用"
            self.lbl_page_subtitle.setText(title)
            self.lbl_page_subtitle.setToolTip(path_text)
            self.lbl_page_subtitle.setStyleSheet(
                f"font-size:12px; color:{Theme.WARNING}; font-weight:600;"
            )
            self.directory_alert_title.setText(title)
            message = path_text
            if detail:
                message = f"{path_text}\n{detail}" if path_text else detail
            self.directory_alert_path.setText(message)
            self.directory_alert_path.setToolTip(path_text)
            histories = [
                value for value in self.conf.get("hist", [])
                if isinstance(value, str) and value.strip()
            ]
            self.btn_directory_history.setVisible(bool(histories))
            self.directory_alert.show()
        elif state == "ready":
            root_label = self.root.name or str(self.root)
            self.lbl_page_subtitle.setText(f"工作目录：{root_label}")
            self.lbl_page_subtitle.setToolTip(str(self.root))
            self.lbl_page_subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
            self.directory_alert.hide()
        else:
            self.lbl_page_subtitle.setText("选择工作目录后开始采集箱号照片")
            self.lbl_page_subtitle.setToolTip("")
            self.lbl_page_subtitle.setStyleSheet(f"font-size:12px; color:{Theme.TEXT_MUTED};")
            self.directory_alert.hide()

    def _invalidate_task_scan(self) -> None:
        self._task_scan_generation += 1
        self._task_scan_task = None
        if hasattr(self, "btn_refresh"):
            self.btn_refresh.setEnabled(True)

    def _start_task_scan(self) -> None:
        if not self.root or self._closing:
            return
        self._task_scan_generation += 1
        generation = self._task_scan_generation
        path_text = str(self.root)
        task = TaskScanTask(
            generation,
            self.root,
            bool(self.conf.get("auto_ocr_enabled", True)),
        )
        task.signals.finished.connect(self._on_task_scan_finished)
        self._task_scan_task = task
        self._task_scan_tasks[generation] = task
        self.btn_refresh.setEnabled(False)

        if self.task_data:
            self._set_work_root_state("ready")
            self.lbl_list_count.setText(f"{self.list.count()} 项 · 正在刷新")
        else:
            self._set_work_root_state("checking", "正在后台读取任务，请稍候")
            self._update_dashboard_counts(0, 0)
            self.render_list(self.search.text())
        QThreadPool.globalInstance().start(task)

    def _on_task_scan_finished(
        self,
        generation: int,
        path_text: str,
        snapshot,
        error: str,
    ) -> None:
        self._task_scan_tasks.pop(generation, None)
        if (
            self._closing
            or generation != self._task_scan_generation
            or str(self.root or "") != path_text
        ):
            return

        self._task_scan_task = None
        self.btn_refresh.setEnabled(True)
        if error or not isinstance(snapshot, dict):
            self.task_data = []
            self._verified_remote_root = None
            detail = f"无法读取目录：{error}" if error else "无法读取目录"
            self._set_work_root_state("unavailable", detail)
            self._update_dashboard_counts(0, 0)
            self.render_list(self.search.text())
            self._report_dirty = True
            if self.stack.currentWidget() is self.report_page:
                self._refresh_collection_report()
            return

        self.task_data = list(snapshot.get("task_data", []))
        state_counts = snapshot.get("state_counts")
        if not isinstance(state_counts, dict):
            state_counts = {
                "all": len(self.task_data),
                "pending": 0,
                "anomaly": 0,
                "complete": 0,
            }
            for summary in self.task_data:
                state = summary.get("task_state", "pending")
                if state in state_counts:
                    state_counts[state] += 1
        ok_count = int(
            snapshot.get(
                "ok_count",
                sum(1 for summary in self.task_data if summary.get("ok")),
            )
        )
        self._set_work_root_state("ready")
        self._update_dashboard_counts(
            len(self.task_data),
            ok_count,
            state_counts,
        )
        self.render_list(self.search.text())
        self._report_dirty = True
        if self.stack.currentWidget() is self.report_page:
            self._refresh_collection_report()

    def _start_work_root_probe(self) -> None:
        if not self.root:
            return
        self._invalidate_task_scan()
        self.task_data = []
        self._work_root_probe_generation += 1
        generation = self._work_root_probe_generation
        path_text = str(self.root)
        self._set_work_root_state("checking", "正在检查网络目录，请稍候")
        self._update_dashboard_counts(0, 0)
        self.render_list(self.search.text())
        self._report_dirty = True
        if self.stack.currentWidget() is self.report_page:
            self._refresh_collection_report()

        def run_probe():
            available, detail = probe_work_root(path_text)
            try:
                self._work_root_probe_signals.finished.emit(
                    generation,
                    path_text,
                    available,
                    detail,
                )
            except RuntimeError:
                pass

        threading.Thread(
            target=run_probe,
            name="PhotoCollectWorkRootProbe",
            daemon=True,
        ).start()
        QTimer.singleShot(
            3000,
            lambda current=generation, path=path_text: self._on_work_root_probe_timeout(
                current,
                path,
            ),
        )

    def _on_work_root_probe_timeout(self, generation: int, path_text: str) -> None:
        if (
            generation != self._work_root_probe_generation
            or str(self.root or "") != path_text
            or self._work_root_state != "checking"
        ):
            return
        self._work_root_probe_generation += 1
        self._verified_remote_root = None
        self._set_work_root_state(
            "unavailable",
            "连接超时，请检查网络后点击刷新重试",
        )
        self.render_list(self.search.text())

    def _on_work_root_probe_finished(
        self,
        generation: int,
        path_text: str,
        available: bool,
        detail: str,
    ) -> None:
        if (
            generation != self._work_root_probe_generation
            or str(self.root or "") != path_text
        ):
            return
        if available:
            self._verified_remote_root = path_text
            self.refresh()
            return
        self._verified_remote_root = None
        self._set_work_root_state("unavailable", detail)
        self._update_dashboard_counts(0, 0)
        self.render_list(self.search.text())
        self._report_dirty = True
        if self.stack.currentWidget() is self.report_page:
            self._refresh_collection_report()

    def _rebuild_directory_history_menu(self) -> None:
        self.directory_history_menu.clear()
        seen = set()
        for value in self.conf.get("hist", []):
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            path = Path(value)
            label = path.name or value
            action = self.directory_history_menu.addAction(label)
            action.setIcon(app_icon(self, "folder", Theme.TEXT_SEC))
            action.setToolTip(value)
            action.setStatusTip(value)
            action.triggered.connect(
                lambda checked=False, selected=value: self._activate_work_root(selected)
            )
        if not seen:
            action = self.directory_history_menu.addAction("没有历史目录")
            action.setEnabled(False)

    def _activate_work_root(self, value: str | Path) -> None:
        value = str(value).strip()
        if not value:
            return
        previous_root = str(self.root) if self.root else ""
        if value != previous_root:
            self._discard_detail_page()
            _thumb_cache.clear()
            self._invalidate_task_scan()
            self.task_data = []
        self._work_root_probe_generation += 1
        self._verified_remote_root = None
        self.root = Path(value)
        self.save_conf()
        self.update_hist()
        self.cb_hist.setCurrentIndex(0)
        self.refresh()

    def _handle_empty_action(self) -> None:
        if self._work_root_state == "ready":
            self.open_batch()
        else:
            self.choose_dir()

    def update_hist(self):
        self.cb_hist.clear()
        if self.root: self.cb_hist.addItem(str(self.root))
        for h in self.conf["hist"]:
            if str(self.root) != h: self.cb_hist.addItem(h)

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择目录")
        if d:
            self._activate_work_root(d)

    def change_hist(self, i):
        value = self.cb_hist.itemText(i).strip()
        if not value:
            return
        self._activate_work_root(value)

    def open_batch(self):
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        d = BatchCreateDialog(self.root, self)
        d.tasks_created.connect(self.refresh)
        if d.exec() == QDialog.Accepted and d.created_count:
            text = f"已创建 {d.created_count} 个任务"
            if d.failed_count:
                text += f"，失败 {d.failed_count} 个"
            self.show_toast(text)

    def open_reqs(self):
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        d = RequirementsDialog(self.root, self)
        d.reqs_changed.connect(self.refresh)
        d.exec()
        # A detail page that is currently open must also see the new
        # global requirements immediately, not only after reopening.
        if self._detail_page is not None:
            self._detail_page.reload_from_disk(invalidate_hashes=False)

    def open_recent_deleted(self):
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        dialog = RecentDeletedDialog(self.root, self)
        dialog.changed.connect(self.refresh)
        dialog.exec()
        current = self.stack.currentWidget()
        if isinstance(current, BoxDetailWindow):
            current.reload_from_disk()

    def open_data_health(self):
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        dialog = DataHealthDialog(self.root, self)
        dialog.exec()
        if dialog.selected_task_name:
            self.open_box_name(dialog.selected_task_name)

    def open_theme_choices(self):
        previous = normalize_theme_name(self.conf.get("theme_name"))
        dialog = ThemeChoiceDialog(previous, self)
        if dialog.exec() != QDialog.Accepted:
            return
        selected = dialog.selected_name()
        self.conf["theme_name"] = selected
        if not self.save_conf():
            self.conf["theme_name"] = previous
            self.show_toast("配色保存失败，原设置保持不变")
            return
        palette = get_theme_palette(selected)
        self.show_toast(f"已选择“{palette.display_name}”，重启应用后完整生效")

    def open_export_naming(self):
        previous_zip = self.conf.get(
            "zip_name_template", DEFAULT_EXPORT_TEMPLATES["zip"]
        )
        previous_excel = self.conf.get(
            "excel_name_template", DEFAULT_EXPORT_TEMPLATES["excel"]
        )
        dialog = ExportNamingDialog(previous_zip, previous_excel, self)
        if dialog.exec() != QDialog.Accepted:
            return
        zip_template, excel_template = dialog.templates()
        self.conf["zip_name_template"] = zip_template
        self.conf["excel_name_template"] = excel_template
        if not self.save_conf():
            self.conf["zip_name_template"] = previous_zip
            self.conf["excel_name_template"] = previous_excel
            self.show_toast("导出文件名模板保存失败，原设置保持不变")
            return
        self.show_toast("导出文件名模板已保存")

    def _selected_task_names(self) -> list[str]:
        names = []
        seen = set()
        for item in self.list.selectedItems():
            name = str(item.data(Qt.UserRole) or "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _update_selection_actions(self) -> None:
        count = len(self.list.selectedItems())
        visible = count > 0
        self.lbl_selection_count.setText(f"已选 {count} 项")
        for widget in (
            self.lbl_selection_count,
            self.btn_selection_export,
            self.btn_selection_reqs,
            self.btn_selection_delete,
        ):
            widget.setVisible(visible)

    def open_batch_requirements(self):
        names = self._selected_task_names()
        if not names:
            self.show_toast("请先选择一个或多个任务")
            return
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return

        metadata = read_meta(self.root / META_NAME)
        loaded_global = metadata.get("__requirements__", DEFAULT_REQS)
        global_reqs = (
            loaded_global if isinstance(loaded_global, list) else DEFAULT_REQS
        )
        overrides = []
        for name in names:
            box_data = metadata.get(name, {})
            override = (
                box_data.get("__requirements__")
                if isinstance(box_data, dict)
                else None
            )
            overrides.append(override if isinstance(override, list) else None)
        common_override = overrides[0] if overrides and all(
            value == overrides[0] for value in overrides
        ) else None
        mixed_note = (
            "当前选择包含不同标准；保存后会统一覆盖所选任务。"
            if overrides and not all(value == overrides[0] for value in overrides)
            else "保存后仅影响所选任务，不改变全局标准。"
        )
        dialog = BoxRequirementsDialog(
            global_reqs,
            common_override,
            self,
            dialog_title="统一调整验收标准",
            heading=f"统一设置 {len(names)} 个任务的照片类型",
            subtitle=mixed_note,
        )
        selected_values = []
        dialog.reqs_changed.connect(lambda value: selected_values.append(value))
        if dialog.exec() != QDialog.Accepted or not selected_values:
            return
        selection = selected_values[-1]

        def apply_batch_requirements(raw):
            for name in names:
                box_data = raw.get(name, {})
                if not isinstance(box_data, dict):
                    box_data = {}
                else:
                    box_data = dict(box_data)
                if selection is None:
                    box_data.pop("__requirements__", None)
                else:
                    box_data["__requirements__"] = list(selection)
                raw[name] = box_data

        try:
            update_meta(self.root / META_NAME, apply_batch_requirements)
        except OSError as exc:
            show_operation_error(
                self,
                "批量验收标准保存失败",
                "所选任务的原标准保持不变，请检查工作目录是否可写。",
                str(exc),
            )
            return
        self.refresh()
        action = "恢复全局标准" if selection is None else "统一验收标准"
        self.show_toast(f"已为 {len(names)} 个任务{action}")

    def _confirm_export_preflight(self, preflight: ExportPreflight) -> bool:
        return ExportPreflightDialog(preflight, self).exec() == QDialog.Accepted

    @staticmethod
    def _open_output_file(path: Path | str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))

    @staticmethod
    def _open_output_folder(path: Path | str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    @staticmethod
    def _export_error_message(kind: str, exc: Exception) -> str:
        detail = str(exc).strip()
        message = f"{kind}导出未完成。请检查保存位置是否可写、目标文件是否被占用。"
        if detail:
            message += f"\n\n原因：{detail[:240]}"
        return message

    def _set_export_controls_enabled(self, enabled: bool) -> None:
        self.btn_export.setEnabled(enabled)
        if hasattr(self, "report_page"):
            self.report_page.btn_export.setEnabled(
                enabled and self.collection_report.total_tasks > 0
            )

    def _start_export_task(self, task, kind: str, initial_label: str) -> None:
        self._export_task = task
        self._export_kind = kind
        self._export_root = Path(self.root)
        self._set_export_controls_enabled(False)

        progress = QProgressDialog(initial_label, "取消", 0, 0, self)
        progress.setWindowTitle("导出资料")
        progress.setWindowModality(Qt.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.setMinimumWidth(420)
        progress.canceled.connect(task.cancel)
        self._export_progress = progress
        progress.show()
        QThreadPool.globalInstance().start(task)

    def export_collection_excel(self, selected_only: bool = False):
        if not self._require_authorization_for_action():
            return
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        if self._export_task is not None:
            self.show_toast("已有导出任务正在进行")
            return
        selected_names = set(self._selected_task_names()) if selected_only else set()
        if selected_only and not selected_names:
            self.show_toast("请先选择要导出的任务")
            return
        self.refresh()
        if selected_only:
            export_data = [
                data for data in self.task_data if data.get("name") in selected_names
            ]
            report = build_collection_report(export_data, ALL_CATEGORIES)
            scope_label = "所选任务"
        else:
            if self._report_dirty:
                self._refresh_collection_report()
            export_data = self.task_data
            report = self.collection_report
            scope_label = "全部任务"
        if not report.total_tasks:
            self.show_toast("当前范围没有可导出的任务")
            return
        if not selected_only and self._report_dirty:
            self._refresh_collection_report()
        preflight = build_export_preflight(
            export_data,
            kind="excel",
            scope=scope_label,
        )
        if not self._confirm_export_preflight(preflight):
            return

        default_name = render_export_filename(
            "excel",
            self.conf.get(
                "excel_name_template", DEFAULT_EXPORT_TEMPLATES["excel"]
            ),
            task_count=preflight.task_count,
            photo_count=preflight.photo_count,
            scope=scope_label,
        ).filename
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出采集看板 Excel",
            str(self.root / default_name),
            "Excel 工作簿 (*.xlsx)",
        )
        if not save_path:
            return

        task = ExcelExportTask(report, save_path, self.root)
        task.signals.progress.connect(self.on_export_progress)
        task.signals.finished.connect(self.on_excel_export_finished)
        task.signals.cancelled.connect(self.on_excel_export_cancelled)
        task.signals.error.connect(self.on_excel_export_error)
        self._excel_export_task_count = report.total_tasks
        self._start_export_task(task, "excel", "正在准备采集看板...")

    def export_handoff(self, scope: str = "all"):
        if not self._require_authorization_for_action():
            return
        if not self.root or not self.root.is_dir():
            self.show_toast("工作目录不可用，请重新选择")
            return
        if self._export_task is not None:
            self.show_toast("已有导出任务正在进行")
            return
        selected_names = set(self._selected_task_names()) if scope == "selected" else set()
        if scope == "selected" and not selected_names:
            self.show_toast("请先选择要导出的任务")
            return
        self.refresh()
        selected = []
        for data in self.task_data:
            if scope == "all":
                selected.append(data)
            elif scope == "complete" and data.get("task_state") == "complete":
                selected.append(data)
            elif scope == "selected" and data.get("name") in selected_names:
                selected.append(data)
        if not selected:
            self.show_toast("没有符合导出条件的任务")
            return
        scope_label = {
            "all": "全部任务",
            "complete": "仅已完成任务",
            "selected": "所选任务",
        }.get(scope, "全部任务")
        preflight = build_export_preflight(
            selected,
            kind="zip",
            scope=scope_label,
        )
        if not self._confirm_export_preflight(preflight):
            return
        default_name = render_export_filename(
            "zip",
            self.conf.get("zip_name_template", DEFAULT_EXPORT_TEMPLATES["zip"]),
            task_count=preflight.task_count,
            photo_count=preflight.photo_count,
            scope=scope_label,
        ).filename
        save_path, _ = QFileDialog.getSaveFileName(
            self, "导出照片交接包", str(self.root / default_name), "ZIP Files (*.zip)"
        )
        if not save_path:
            return
        if not save_path.lower().endswith(".zip"):
            save_path += ".zip"

        state_labels = {"complete": "已完成", "pending": "待补", "anomaly": "异常"}
        records = [
            TaskExportRecord(
                name=data["name"],
                size_type=data.get("ocr_size_type", ""),
                ocr_status=data.get("ocr_status", ""),
                task_status=state_labels.get(data.get("task_state"), "待补"),
                photo_count=data.get("pic_cnt", 0),
                missing=tuple(data.get("missing", [])),
                last_updated=data.get("last_updated", ""),
                required_categories=tuple(data.get("required_categories", [])),
                missing_category_ids=tuple(data.get("missing_category_ids", [])),
            )
            for data in selected
        ]
        category_names = {index: name for index, name in ALL_CATEGORIES}
        task = ExportTask(self.root, save_path, records, category_names, optimize_export_image)
        task.signals.progress.connect(self.on_export_progress)
        task.signals.finished.connect(self.on_export_finished)
        task.signals.cancelled.connect(self.on_export_cancelled)
        task.signals.error.connect(self.on_export_error)
        self._start_export_task(task, "zip", "正在准备照片交接包...")

    def on_export_progress(self, current: int, total: int, label: str):
        progress = self._export_progress
        if progress is None:
            return
        progress.setRange(0, max(total, 1))
        progress.setValue(min(current, max(total, 1)))
        phase = f"正在写入：{label}" if self._export_kind == "zip" else label
        progress.setLabelText(phase)

    def _close_export_ui(self):
        if self._export_progress is not None:
            self._export_progress.close()
            self._export_progress.deleteLater()
        self._export_progress = None
        self._export_task = None
        self._export_root = None
        self._export_kind = None
        self._excel_export_task_count = 0
        self._set_export_controls_enabled(True)

    def on_excel_export_finished(self, output):
        task_count = self._excel_export_task_count
        self._close_export_ui()
        self.show_toast(
            f"已导出 {task_count} 个任务的采集看板",
            "打开文件",
            lambda path=output: self._open_output_file(path),
            8000,
            "打开文件夹",
            lambda path=output: self._open_output_folder(path),
        )
        log.info("Collection report exported: %s", output)

    def on_excel_export_cancelled(self):
        self._close_export_ui()
        self.show_toast("已取消 Excel 导出")

    def on_excel_export_error(self, message: str):
        self._close_export_ui()
        show_operation_error(
            self,
            "Excel 导出失败",
            self._export_error_message("Excel", RuntimeError(message)),
            str(message),
        )

    def on_export_finished(self, summary):
        export_root = self._export_root
        self._close_export_ui()

        def increment_export_count(raw):
            count = raw.get("__export_count__", 0)
            raw["__export_count__"] = count + 1 if isinstance(count, int) else 1

        try:
            if export_root is None:
                raise OSError("无法确定本次导出的原工作目录")
            update_meta(export_root / META_NAME, increment_export_count)
        except OSError as exc:
            show_operation_error(
                self,
                "导出记录更新失败",
                "照片交接包已经成功生成，仅导出次数未能写回原工作目录。",
                str(exc),
            )
        message = f"已导出 {summary.task_count} 个任务、{summary.photo_count} 张照片"
        if summary.optimized_photo_count:
            before_mib = summary.source_photo_bytes / (1024 * 1024)
            after_mib = summary.exported_photo_bytes / (1024 * 1024)
            message += f"，优化 {summary.optimized_photo_count} 张（{before_mib:.0f} → {after_mib:.0f} MiB）"
        self.show_toast(
            message,
            "打开文件夹",
            lambda path=summary.output_path: self._open_output_folder(path),
            8000,
        )
        self.log(
            f"交接包导出成功: {summary.output_path}; "
            f"photos={summary.photo_count}, optimized={summary.optimized_photo_count}, "
            f"source_bytes={summary.source_photo_bytes}, exported_bytes={summary.exported_photo_bytes}"
        )

    def on_export_cancelled(self):
        self._close_export_ui()
        self.show_toast("已取消导出")

    def on_export_error(self, message: str):
        self._close_export_ui()
        show_operation_error(
            self,
            "ZIP 导出失败",
            self._export_error_message("ZIP", RuntimeError(message)),
            str(message),
        )

    def set_filter(self, value: str) -> None:
        if value not in self.filter_buttons:
            return
        self._current_filter = value
        self.filter_buttons[value].setChecked(True)
        self.save_conf()
        self.render_list(self.search.text())

    def set_sort(self, value: str) -> None:
        if value not in {"status", "name", "updated"}:
            return
        self._sort_order = value
        for key, action in self.sort_actions.items():
            action.setChecked(key == value)
        labels = {"status": "状态优先", "name": "按箱号", "updated": "最近更新"}
        self.btn_sort.setToolTip(f"排序：{labels[value]}")
        self.save_conf()
        self.render_list(self.search.text())

    def open_selected_task(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.open_box(item)

    def list_ctx_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        if not it: return
        if not it.isSelected():
            self.list.clearSelection()
            it.setSelected(True)
            self.list.setCurrentItem(it)
        names = self._selected_task_names()
        m = styled_menu()
        act_open = m.addAction("打开")
        act_open.setIcon(app_icon(self, "open"))
        m.addSeparator()
        act_zip = m.addAction(f"导出所选 {len(names)} 项为 ZIP")
        act_zip.setIcon(app_icon(self, "export"))
        act_excel = m.addAction(f"导出所选 {len(names)} 项为 Excel")
        act_excel.setIcon(app_icon(self, "chart"))
        act_reqs = m.addAction("统一调整验收标准")
        act_reqs.setIcon(app_icon(self, "settings"))
        m.addSeparator()
        act_del = m.addAction(
            "删除任务" if len(names) == 1 else f"删除所选 {len(names)} 项"
        )
        act_del.setIcon(app_icon(self, "delete", Theme.ERROR))
        chosen = m.exec(self.list.mapToGlobal(pos))
        if chosen == act_open:
            self.open_box(it)
        elif chosen == act_zip:
            self.export_handoff("selected")
        elif chosen == act_excel:
            self.export_collection_excel(True)
        elif chosen == act_reqs:
            self.open_batch_requirements()
        elif chosen == act_del:
            self._delete_tasks(names)

    def _delete_selected_task(self) -> None:
        if not self._require_authorization_for_action():
            return
        self._delete_tasks(self._selected_task_names())

    def _delete_task(self, name: str) -> None:
        self._delete_tasks([name] if name else [])

    def _delete_tasks(self, names) -> None:
        if not self._require_authorization_for_action():
            return
        names = list(dict.fromkeys(str(name) for name in names if str(name)))
        if not names:
            return
        if len(names) == 1:
            prompt = f"将任务 '{names[0]}' 及其照片移到最近删除？"
        else:
            preview = "、".join(names[:5])
            suffix = "…" if len(names) > 5 else ""
            prompt = (
                f"将所选 {len(names)} 个任务及其照片移到最近删除？\n\n"
                f"{preview}{suffix}"
            )
        reply = QMessageBox.question(self, "确认删除",
            prompt,
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        store = TrashStore(self.root)
        entries = []
        try:
            for name in names:
                entries.append(store.trash_task(name))
            _thumb_cache.clear()
            self.refresh()
            def undo_tasks():
                restored = []
                try:
                    for entry in reversed(entries):
                        store.restore(entry["id"])
                        restored.append(entry)
                    self.refresh()
                    self.show_toast(f"已恢复 {len(restored)} 个任务")
                except Exception as exc:
                    self.refresh()
                    show_operation_error(
                        self,
                        "批量恢复未全部完成",
                        "已恢复的任务保持可用，其余任务仍在最近删除中。",
                        str(exc),
                    )
            message = (
                f"任务 {names[0]} 已移到最近删除"
                if len(names) == 1
                else f"{len(names)} 个任务已移到最近删除"
            )
            self.show_toast(message, "撤销", undo_tasks)
        except Exception as e:
            rollback_errors = []
            for entry in reversed(entries):
                try:
                    store.restore(entry["id"])
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            self.refresh()
            log.warning("Delete task failed: %s", e)
            detail = str(e)
            if rollback_errors:
                detail += "\n回退异常：" + "；".join(rollback_errors)
            show_operation_error(
                self,
                "批量删除未完成",
                "已自动回退成功步骤；请运行数据检查确认工作目录状态。",
                detail,
            )

    def _update_dashboard_counts(self, total: int, complete: int, state_counts=None) -> None:
        counts = state_counts or {"all": total, "pending": 0, "anomaly": 0, "complete": complete}
        self.lbl_tot.setText(str(total))
        self.lbl_ok.setText(str(complete))
        self.lbl_bad.setText(str(max(0, total - complete)))
        for key, button in self.filter_buttons.items():
            button.setText(f"{button.property('filterLabel')} {counts.get(key, 0)}")
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(complete)

    def _update_dashboard_counts_from_tasks(self) -> None:
        counts = {"all": len(self.task_data), "pending": 0, "anomaly": 0, "complete": 0}
        for summary in self.task_data:
            state = summary.get("task_state", "pending")
            if state in counts:
                counts[state] += 1
        self._update_dashboard_counts(len(self.task_data), counts["complete"], counts)

    def _filtered_task_data(self, key=""):
        key = str(key).lower()
        filtered = [
            data for data in self.task_data
            if key in data["filter"]
            and (self._current_filter == "all" or data["task_state"] == self._current_filter)
        ]
        if self._sort_order == "status":
            priority = {"anomaly": 0, "pending": 1, "complete": 2}
            filtered.sort(key=lambda data: (priority.get(data["task_state"], 9), data["name"]))
        elif self._sort_order == "updated":
            filtered.sort(key=lambda data: (-data.get("updated_ts", 0), data["name"]))
        else:
            filtered.sort(key=lambda data: data["name"])
        return filtered

    def _materialize_task_item(self, item) -> None:
        if (
            self._closing
            or item is None
            or self.list.row(item) < 0
            or self.list.itemWidget(item) is not None
        ):
            return
        data = item.data(self.TASK_DATA_ROLE)
        if isinstance(data, dict):
            self.list.setItemWidget(item, TaskRowWidget(data, self.list))

    def _materialize_task_row_batch(
        self,
        generation: int,
        start_row: int,
    ) -> None:
        if self._closing or generation != self._render_generation:
            return
        row = max(0, int(start_row))
        count = self.list.count()
        deadline = time.perf_counter() + self.TASK_RENDER_BUDGET_MS / 1000
        while row < count:
            self._materialize_task_item(self.list.item(row))
            row += 1
            if time.perf_counter() >= deadline:
                break
        if row < count and generation == self._render_generation:
            QTimer.singleShot(
                0,
                lambda current=generation, next_row=row: self._materialize_task_row_batch(
                    current,
                    next_row,
                ),
            )

    def _materialize_visible_task_rows(self, _value=None) -> None:
        if self._closing or not hasattr(self, "list") or not self.list.count():
            return
        viewport = self.list.viewport()
        top_item = self.list.itemAt(QPoint(8, 8))
        bottom_item = self.list.itemAt(
            QPoint(8, max(8, viewport.height() - 8))
        )
        if top_item is not None:
            first_row = self.list.row(top_item)
        else:
            first_row = max(
                0,
                self.list.verticalScrollBar().value() // self.TASK_ROW_HEIGHT,
            )
        if bottom_item is not None:
            last_row = self.list.row(bottom_item)
        else:
            visible_rows = max(
                1,
                viewport.height() // self.TASK_ROW_HEIGHT + 2,
            )
            last_row = min(self.list.count() - 1, first_row + visible_rows)
        for row in range(first_row, last_row + 1):
            self._materialize_task_item(self.list.item(row))

    def _insert_task_row(
        self,
        row: int,
        data: dict,
        *,
        materialize: bool = True,
    ):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, data["name"])
        item.setData(self.TASK_DATA_ROLE, data)
        item.setSizeHint(QSize(100, self.TASK_ROW_HEIGHT))
        self.list.insertItem(row, item)
        if materialize:
            self._materialize_task_item(item)
        return item

    def _update_task_list_state(self, visible_count: int) -> None:
        self.lbl_list_count.setText(f"{visible_count} 项")
        if self._work_root_state == "unselected":
            self.empty_title.setText("还未选择工作目录")
            self.empty_detail.setText("选择一个文件夹，用于保存箱号任务和照片")
            self.empty_action.setText("选择工作目录")
            self.empty_action.setVisible(True)
        elif self._work_root_state == "checking":
            self.empty_title.setText("正在连接工作目录")
            self.empty_detail.setText(str(self.root) if self.root else "正在检查目录")
            self.empty_action.setVisible(False)
        elif self._work_root_state == "unavailable":
            self.empty_title.setText("上次工作目录不可用")
            self.empty_detail.setText(str(self.root) if self.root else "请重新选择工作目录")
            self.empty_action.setText("重新选择目录")
            self.empty_action.setVisible(True)
        elif not self.task_data:
            self.empty_title.setText("还没有任务")
            self.empty_detail.setText("批量创建箱号后即可开始采集照片")
            self.empty_action.setText("批量新建")
            self.empty_action.setVisible(True)
        else:
            self.empty_title.setText("没有符合当前条件的任务")
            self.empty_detail.setText("调整筛选条件或搜索内容后再试")
            self.empty_action.setVisible(False)
        self.list_stack.setCurrentIndex(0 if visible_count else 1)

    def _update_task_row(self, box_name: str) -> None:
        desired = self._filtered_task_data(self.search.text())
        desired_index = next(
            (index for index, data in enumerate(desired) if data["name"] == box_name),
            -1,
        )
        existing_index = next(
            (
                index for index in range(self.list.count())
                if self.list.item(index).data(Qt.UserRole) == box_name
            ),
            -1,
        )
        was_selected = (
            self.list.currentItem() is not None
            and self.list.currentItem().data(Qt.UserRole) == box_name
        )
        if existing_index >= 0:
            old_item = self.list.item(existing_index)
            old_widget = self.list.itemWidget(old_item)
            if old_widget is not None:
                self.list.removeItemWidget(old_item)
                old_widget.deleteLater()
            self.list.takeItem(existing_index)

        if desired_index >= 0:
            new_item = self._insert_task_row(desired_index, desired[desired_index])
            if was_selected:
                self.list.setCurrentItem(new_item)
        self._update_task_list_state(len(desired))
        generation = self._render_generation
        QTimer.singleShot(
            0,
            lambda current=generation: self._materialize_task_row_batch(
                current,
                0,
            ),
        )

    def _mark_detail_dirty(self, box_name: str) -> None:
        if box_name:
            self._dirty_task_names.add(str(box_name))
            self._report_dirty = True

    def _apply_pending_task_updates(self) -> None:
        names = sorted(self._dirty_task_names)
        if not names:
            return
        self._dirty_task_names.difference_update(names)
        if self.root and is_remote_work_root(self.root):
            self.refresh()
            return
        if not self.root or not self.root.exists():
            self.refresh()
            return

        meta = read_meta(self.root / META_NAME)
        loaded_reqs = meta.get("__requirements__", DEFAULT_REQS)
        reqs = loaded_reqs if isinstance(loaded_reqs, list) else DEFAULT_REQS
        for box_name in names:
            box_dir = self.root / box_name
            current_index = next(
                (index for index, data in enumerate(self.task_data) if data["name"] == box_name),
                -1,
            )
            if box_dir.is_dir():
                summary = build_task_summary(
                    box_dir,
                    meta.get(box_name, {}),
                    reqs,
                    require_ocr=bool(self.conf.get("auto_ocr_enabled", True)),
                )
                if current_index >= 0:
                    self.task_data[current_index] = summary
                else:
                    self.task_data.append(summary)
            elif current_index >= 0:
                self.task_data.pop(current_index)

        self._update_dashboard_counts_from_tasks()
        for box_name in names:
            self._update_task_row(box_name)
        self._report_dirty = True
        if self.stack.currentWidget() is self.report_page:
            self._refresh_collection_report()

    def refresh(self):
        if not self.root:
            self._invalidate_task_scan()
            self.task_data = []
            self._set_work_root_state("unselected")
            self._update_dashboard_counts(0, 0)
            self.render_list(self.search.text())
            self._report_dirty = True
            if self.stack.currentWidget() is self.report_page:
                self._refresh_collection_report()
            return
        remote_root = is_remote_work_root(self.root)
        if remote_root and self._verified_remote_root != str(self.root):
            self._start_work_root_probe()
            return
        if remote_root:
            self._start_task_scan()
            return

        self._invalidate_task_scan()
        self.task_data = []
        if not self.root.is_dir():
            self._set_work_root_state("unavailable")
            self._update_dashboard_counts(0, 0)
            self.render_list(self.search.text())
            self._report_dirty = True
            if self.stack.currentWidget() is self.report_page:
                self._refresh_collection_report()
            return

        try:
            snapshot = scan_task_summaries(
                self.root,
                require_ocr=bool(self.conf.get("auto_ocr_enabled", True)),
            )
        except OSError as exc:
            log.exception("Unable to scan work directory %s", self.root)
            self._set_work_root_state("unavailable", f"无法读取目录：{exc}")
            self._update_dashboard_counts(0, 0)
            self.render_list(self.search.text())
            self._report_dirty = True
            if self.stack.currentWidget() is self.report_page:
                self._refresh_collection_report()
            return

        self.task_data = list(snapshot["task_data"])
        self._set_work_root_state("ready")
        self._update_dashboard_counts(
            len(self.task_data),
            int(snapshot["ok_count"]),
            snapshot["state_counts"],
        )
        self.render_list(self.search.text())
        self._report_dirty = True
        if self.stack.currentWidget() is self.report_page:
            self._refresh_collection_report()

    def render_list(self, key=''):
        self._render_generation += 1
        generation = self._render_generation
        filtered = self._filtered_task_data(key)
        signals_were_blocked = self.list.blockSignals(True)
        self.list.setUpdatesEnabled(False)
        try:
            self.list.clear()
            for row, data in enumerate(filtered):
                self._insert_task_row(
                    row,
                    data,
                    materialize=False,
                )
            initial_count = min(
                len(filtered),
                max(
                    self.INITIAL_TASK_WIDGETS,
                    self.list.viewport().height() // self.TASK_ROW_HEIGHT + 2,
                ),
            )
            for row in range(initial_count):
                self._materialize_task_item(self.list.item(row))
        finally:
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(signals_were_blocked)
        self._update_task_list_state(len(filtered))
        self._update_selection_actions()
        if initial_count < len(filtered):
            QTimer.singleShot(
                0,
                lambda current=generation, next_row=initial_count: self._materialize_task_row_batch(
                    current,
                    next_row,
                ),
            )

    def open_box(self, item):
        if item is None:
            return
        box_name = item.data(Qt.UserRole)
        if box_name:
            self.open_box_name(str(box_name))

    def open_box_name(self, box_name: str):
        try:
            if not self.root or not (self.root / box_name).is_dir():
                raise FileNotFoundError(f"任务不存在：{box_name}")
            current = self.stack.currentWidget()
            if current in (self.dashboard_page, self.report_page):
                self._detail_return_page = current
            elif not hasattr(self, "_detail_return_page"):
                self._detail_return_page = self.dashboard_page
            self._discard_detail_page()

            detail = BoxDetailWindow(self.root, box_name, self)
            detail.data_changed.connect(lambda name=box_name: self._mark_detail_dirty(name))
            detail.back_requested.connect(self.on_back_to_dashboard)
            detail.navigate_requested.connect(self.open_adjacent_box)
            names = self._navigation_names()
            if box_name in names:
                detail.set_navigation(names.index(box_name) + 1, len(names))
            self._detail_page = detail
            self.stack.addWidget(detail)
            self.stack.setCurrentWidget(detail)
            if self._detail_return_page is self.report_page:
                self.btn_report.setChecked(True)
            else:
                self.btn_tasks.setChecked(True)
        except Exception as e:
            log.exception("Unable to open task detail")
            show_operation_error(
                self,
                "无法打开任务",
                "任务详情没有打开。请确认任务目录仍存在，并运行数据检查。",
                str(e),
            )

    def _navigation_names(self) -> list:
        """Box order for 上一箱/下一箱: the task list as filtered and sorted now."""
        names = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        return [str(name) for name in names if name]

    def open_adjacent_box(self, step: int) -> None:
        detail = self._detail_page
        if detail is None:
            return
        if detail.has_pending_saves():
            self.show_toast("照片还在保存，保存完再切换箱子")
            return
        names = self._navigation_names()
        if detail.box_name not in names:
            self.show_toast("当前箱子不在任务列表的筛选结果里")
            return
        index = names.index(detail.box_name) + int(step)
        if not 0 <= index < len(names):
            self.show_toast("已经是第一个箱子" if step < 0 else "已经是最后一个箱子")
            return
        target = names[index]
        self.open_box_name(target)
        item = self.list.item(index)
        if item is not None and item.data(Qt.UserRole) == target:
            self.list.setCurrentItem(item)
            self.list.scrollToItem(item)

    def on_back_to_dashboard(self):
        target = getattr(self, "_detail_return_page", self.dashboard_page)
        self._discard_detail_page()
        self.stack.setCurrentWidget(target)
        if target is self.report_page:
            self.btn_report.setChecked(True)
        else:
            self.btn_tasks.setChecked(True)
        if self._dirty_task_names:
            QTimer.singleShot(0, self._apply_pending_task_updates)

    def toggle_sidebar(self):
        """折叠/展开侧边栏"""
        if self._sidebar_auto_collapsed:
            self._sidebar_auto_collapsed = False
            self._sidebar_user_collapsed = False
        else:
            self._sidebar_user_collapsed = not self._sidebar_user_collapsed
        self._apply_sidebar_state()
        self.save_conf()

    def _apply_sidebar_state(self):
        """根据折叠状态更新侧边栏"""
        self._sidebar_collapsed = self._sidebar_user_collapsed or self._sidebar_auto_collapsed
        collapsed = self._sidebar_collapsed
        for w in self._sidebar_widgets:
            w.setVisible(not collapsed)
        for btn, (full_text, icon_text) in self._sidebar_buttons.items():
            btn.setText(icon_text if collapsed else full_text)
            btn.setToolTip(full_text)
            style_sidebar_button(btn, collapsed)
        if collapsed:
            self.sidebar.setFixedWidth(52)
            self.btn_collapse.setIcon(app_icon(self.btn_collapse, "expand", Theme.TEXT_SEC))
            self.btn_collapse.setToolTip("展开侧边栏")
        else:
            self.sidebar.setFixedWidth(232)
            self.btn_collapse.setIcon(app_icon(self.btn_collapse, "collapse", Theme.TEXT_SEC))
            self.btn_collapse.setToolTip("折叠侧边栏")

    def resizeEvent(self, event):
        """窗口大小变化时自动折叠/展开侧边栏"""
        super().resizeEvent(event)
        if hasattr(self, "toast"):
            self.toast.reposition()
        auto_collapsed = self.width() < self.SIDEBAR_COLLAPSE_THRESHOLD
        if auto_collapsed != self._sidebar_auto_collapsed:
            self._sidebar_auto_collapsed = auto_collapsed
            if hasattr(self, "sidebar"):
                self._apply_sidebar_state()
        if hasattr(self, "primary_commands"):
            self._apply_command_bar_layout()
        if hasattr(self, "dashboard_summary_layout"):
            self._apply_dashboard_summary_layout()

    def closeEvent(self, event):
        if self._export_task is not None:
            reply = QMessageBox.question(self, "导出进行中", "取消当前导出并关闭程序？")
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self._export_task.cancel()
            QThreadPool.globalInstance().waitForDone(10000)
        if self._batch_ocr_task is not None:
            self._batch_ocr_task.cancel()
        self._closing = True
        if self._update_download_task is not None:
            self._update_download_task.cancel()
        self._update_notification_stop.set()
        self._update_thread_pool.clear()
        if not self._update_thread_pool.waitForDone(15000):
            log.warning("Update worker did not stop within the shutdown deadline")
        self._update_check_task = None
        self._update_download_task = None
        self._render_generation += 1
        self._task_scan_generation += 1
        self._task_scan_task = None
        try:
            self.save_conf()
        except OSError:
            log.exception("Failed to save application configuration")
        super().closeEvent(event)


if __name__ == '__main__':
    post_update_health = None
    try:
        post_update_health = consume_post_update_health_args(sys.argv)
    except UpdateError:
        log.exception("Invalid post-update health arguments")
    if "--acceptance-smoke" in sys.argv:
        sys.exit(run_acceptance_smoke_cli(sys.argv))
    if "--ocr-smoke" in sys.argv:
        sys.exit(run_ocr_smoke_cli(sys.argv))
    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    configure_app_style(app, load_saved_theme_name())
    try:
        window_icon_path = resource_path(APP_LOGO_ICO)
        if window_icon_path.exists():
            app.setWindowIcon(QIcon(str(window_icon_path)))
    except Exception:
        pass
    w = ContainerApp()
    w.show()
    if getattr(sys, "frozen", False):
        update_failure = consume_update_failure()
        if update_failure is not None:
            def report_update_failure(result=update_failure):
                recovered = result.get("recovered")
                if recovered is True:
                    summary = f"v{result['version']} 未能完成安装，已恢复原版本。照片和任务数据没有改变。"
                elif recovered is False:
                    summary = f"v{result['version']} 安装及自动恢复均失败，请关闭程序并联系管理员。照片和任务数据没有改变。"
                else:
                    summary = f"v{result['version']} 未能完成安装，请检查当前程序版本。照片和任务数据没有改变。"
                show_operation_error(
                    w,
                    "更新未完成",
                    summary,
                    f"原因：{result['message']}\n日志：{result['log_path']}",
                )

            QTimer.singleShot(0, report_update_failure)
    if post_update_health is not None:
        marker_path, health_token = post_update_health

        def report_healthy_startup():
            # The health marker means the new executable initialized its GUI.
            # Authorization independently controls business actions and must
            # not turn a temporary Internet outage into an update rollback.
            try:
                write_post_update_health(
                    marker_path,
                    health_token,
                    APP_VERSION,
                )
            except Exception:
                log.exception("Unable to write post-update health marker")

        QTimer.singleShot(0, report_healthy_startup)
    # Old updater helpers leave the rollback copy for the new application to
    # clean. Waiting longer than their health deadline avoids racing rollback.
    if getattr(sys, "frozen", False):
        QTimer.singleShot(60000, cleanup_successful_update_backup)
    sys.exit(app.exec())
