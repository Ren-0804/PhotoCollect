"""Train the photo-slot classifier used by the auto-sort drop zone (developer tool).

Needs torch and torchvision, which are not part of the app.  Photos are read in
place from existing work roots; only small resized copies go to the work folder,
which must be outside the repository.

    python tools/train_photo_classifier.py manifest WORK --roots-file roots.txt --holdout 项目A --holdout 项目B
    python tools/train_photo_classifier.py cache WORK
    python tools/train_photo_classifier.py train WORK --epochs 12
    python tools/train_photo_classifier.py evaluate WORK --split test

roots.txt holds one work root per line.  Labels come from the saved file names
(<box>-<slot>[_k].ext): slots 1-5 are classes, slots 6-8 become OTHER.  Boxes
whose folder path contains a --holdout text form the test split; 15% of the
other boxes (by box number) form the validation split.  `train` writes
models/photo_classifier.onnx and models/photo_classifier.json in WORK; copy both
into the repository's models/ folder (git-ignored) before building the EXE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photo_classifier import (  # noqa: E402
    CONFIG_NAME,
    DEFAULT_CLASSES,
    INPUT_HEIGHT,
    INPUT_WIDTH,
    MODEL_NAME,
    OTHER,
    PhotoClassifier,
    load_photo,
    model_input,
)


BOX_DIR_RE = re.compile(r"^([A-Z]{4}\d{7})")
PHOTO_RE = re.compile(r"^[A-Z]{4}\d{7}-([1-8])(?:_(\d+))?\.(?:jpg|jpeg|png|bmp)$", re.I)
CACHE_SIZE = (INPUT_WIDTH * 3 // 2, INPUT_HEIGHT * 3 // 2)
VALIDATION_PERCENT = 15
TARGET_PRECISION = 0.99
MIN_THRESHOLD = 0.80


def label_for_slot(slot: int) -> int:
    return slot if slot <= 5 else OTHER


def split_for(box: str, path: str, holdout: list[str]) -> str:
    if any(text in path for text in holdout):
        return "test"
    return "val" if zlib.crc32(box.encode("ascii")) % 100 < VALIDATION_PERCENT else "train"


def scan_root(root: Path) -> list[dict]:
    records = []
    for box_dir in sorted((entry for entry in os.scandir(root) if entry.is_dir()), key=lambda entry: entry.name):
        match = BOX_DIR_RE.match(box_dir.name)
        if not match:
            continue
        for entry in os.scandir(box_dir.path):
            photo = PHOTO_RE.match(entry.name)
            if not photo or not entry.is_file():
                continue
            size = entry.stat().st_size
            if size == 0:
                continue
            records.append({
                "path": entry.path,
                "box": match.group(1).upper(),
                "slot": int(photo.group(1)),
                "copy": int(photo.group(2) or 1),
                "size": size,
            })
    return records


def build_manifest(args) -> None:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    roots = [line.strip() for line in Path(args.roots_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    with ThreadPoolExecutor(8) as pool:
        scanned = list(pool.map(lambda root: scan_root(Path(root)), roots))
    seen = set()
    test_boxes = set()
    records = []
    for root_records in scanned:
        for record in root_records:
            key = (record["box"], record["slot"], record["copy"], record["size"])
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            if any(text in record["path"] for text in args.holdout):
                test_boxes.add(record["box"])
    for index, record in enumerate(records):
        record["id"] = index
        record["label"] = label_for_slot(record["slot"])
        record["split"] = "test" if record["box"] in test_boxes else split_for(record["box"], record["path"], [])
    (work / "manifest.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    counts = Counter((record["split"], record["label"]) for record in records)
    for split in ("train", "val", "test"):
        boxes = len({record["box"] for record in records if record["split"] == split})
        row = ", ".join(f"{label}:{counts[(split, label)]}" for label in DEFAULT_CLASSES)
        print(f"{split}: {boxes} boxes, {row}")


def load_manifest(work: Path) -> list[dict]:
    return json.loads((work / "manifest.json").read_text(encoding="utf-8"))


def cache_path(work: Path, record: dict) -> Path:
    return work / "cache" / f"{record['id']}.jpg"


def build_cache(args) -> None:
    work = Path(args.work)
    (work / "cache").mkdir(exist_ok=True)
    records = [record for record in load_manifest(work) if not cache_path(work, record).exists()]
    failures = []

    def cache_one(record):
        try:
            image = load_photo(record["path"]).resize(CACHE_SIZE)
            image.save(cache_path(work, record), quality=92)
        except Exception as exc:  # unreadable or truncated photo
            failures.append((record["path"], repr(exc)))

    started = time.time()
    with ThreadPoolExecutor(8) as pool:
        for done, _ in enumerate(pool.map(cache_one, records), 1):
            if done % 500 == 0:
                print(f"{done}/{len(records)} {time.time() - started:.0f}s", flush=True)
    (work / "cache_failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"cached {len(records) - len(failures)}, failed {len(failures)}")


class CachedPhotos:
    """Map-style dataset over the resized cache.  Module level so that Windows
    DataLoader workers (spawned processes) can unpickle it."""

    def __init__(self, work: Path, items: list[dict], train: bool):
        self.work = Path(work)
        self.items = [item for item in items if cache_path(self.work, item).exists()]
        self.train = train
        self.class_index = {category: index for index, category in enumerate(DEFAULT_CLASSES)}
        self._augment = None

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        import torch
        from PIL import Image
        from torchvision import transforms

        item = self.items[index]
        with Image.open(cache_path(self.work, item)) as image:
            image = image.convert("RGB")
        if self.train:
            if self._augment is None:
                self._augment = transforms.Compose([
                    transforms.RandomResizedCrop((INPUT_HEIGHT, INPUT_WIDTH), scale=(0.7, 1.0), ratio=(0.7, 0.8)),
                    transforms.RandomHorizontalFlip(),
                    transforms.ColorJitter(0.3, 0.3, 0.2, 0.03),
                ])
            image = self._augment(image)
        return torch.from_numpy(model_input(image)[0]), self.class_index[item["label"]]


def _predict_batches(model, loader):
    import torch

    probabilities, labels = [], []
    model.eval()
    with torch.no_grad():
        for images, targets in loader:
            probabilities.append(torch.softmax(model(images), dim=1).numpy())
            labels.append(targets.numpy())
    return np.concatenate(probabilities), np.concatenate(labels)


def pick_threshold(probabilities: np.ndarray, labels: np.ndarray, target: float = TARGET_PRECISION) -> float:
    """Lowest confidence, but at least MIN_THRESHOLD, at which the confident
    predictions are at least `target` correct.  The floor keeps borderline
    photos (e.g. a nearly full box) in the "which slot?" prompt even when the
    validation set is too clean to show such cases."""
    confidence = probabilities.max(axis=1)
    correct = probabilities.argmax(axis=1) == labels
    for threshold in np.arange(MIN_THRESHOLD, 0.996, 0.005):
        chosen = confidence >= threshold
        if chosen.any() and correct[chosen].mean() >= target:
            return round(float(threshold), 3)
    return 0.995


def train(args) -> None:
    import torch
    from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

    torch.manual_seed(0)
    torch.set_num_threads(os.cpu_count() or 4)
    work = Path(args.work)
    records = load_manifest(work)
    train_set = CachedPhotos(work, [r for r in records if r["split"] == "train"], train=True)
    val_set = CachedPhotos(work, [r for r in records if r["split"] == "val"], train=False)
    train_loader = torch.utils.data.DataLoader(
        train_set, batch_size=32, shuffle=True, num_workers=3, persistent_workers=True
    )
    val_loader = torch.utils.data.DataLoader(val_set, batch_size=64, num_workers=2, persistent_workers=True)

    model = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1)
    model.classifier[3] = torch.nn.Linear(model.classifier[3].in_features, len(DEFAULT_CLASSES))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=args.lr, epochs=args.epochs, steps_per_epoch=len(train_loader)
    )
    loss_fn = torch.nn.CrossEntropyLoss()
    best = (float("inf"), None)
    print(f"train {len(train_set)} / val {len(val_set)} photos", flush=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        started, total, seen = time.time(), 0.0, 0
        for images, targets in train_loader:
            optimizer.zero_grad()
            loss = loss_fn(model(images), targets)
            loss.backward()
            optimizer.step()
            scheduler.step()
            total += loss.item() * len(targets)
            seen += len(targets)
        probabilities, labels = _predict_batches(model, val_loader)
        val_loss = float(-np.log(probabilities[np.arange(len(labels)), labels] + 1e-9).mean())
        accuracy = float((probabilities.argmax(axis=1) == labels).mean())
        print(
            f"epoch {epoch}: train loss {total / seen:.4f}, val loss {val_loss:.4f}, "
            f"val acc {accuracy:.4f}, {time.time() - started:.0f}s",
            flush=True,
        )
        if val_loss < best[0]:
            best = (val_loss, {key: value.clone() for key, value in model.state_dict().items()})
    model.load_state_dict(best[1])
    probabilities, labels = _predict_batches(model, val_loader)
    threshold = pick_threshold(probabilities, labels)

    out = work / "models"
    out.mkdir(exist_ok=True)
    model.eval()
    torch.onnx.export(
        model,
        (torch.zeros(1, 3, INPUT_HEIGHT, INPUT_WIDTH),),
        str(out / MODEL_NAME),
        dynamo=False,
        input_names=["image"],
        output_names=["logits"],
        opset_version=17,
    )
    config = {
        "classes": list(DEFAULT_CLASSES),
        "input": [INPUT_WIDTH, INPUT_HEIGHT],
        "threshold": threshold,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "train_photos": len(train_set),
        "val_photos": len(val_set),
        "val_accuracy": round(float((probabilities.argmax(axis=1) == labels).mean()), 4),
    }
    (out / CONFIG_NAME).write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(json.dumps(config, indent=2))


def evaluate(args) -> None:
    work = Path(args.work)
    classifier = PhotoClassifier.from_directory(args.model or work / "models")
    if classifier is None:
        raise SystemExit("model not found")
    threshold = args.threshold if args.threshold is not None else classifier.threshold
    records = [r for r in load_manifest(work) if r["split"] == args.split]
    results = []

    def predict(record):
        try:
            return record, classifier.predict(record["path"])
        except Exception:
            return record, None

    started = time.time()
    with ThreadPoolExecutor(4) as pool:
        results = [item for item in pool.map(predict, records) if item[1] is not None]
    elapsed = time.time() - started
    classes = list(DEFAULT_CLASSES)
    confusion = np.zeros((len(classes), len(classes)), dtype=int)
    placed = placed_right = asked = asked_top3 = manual = manual_right = 0
    for record, prediction in results:
        confusion[classes.index(record["label"]), classes.index(prediction.category)] += 1
        if prediction.confidence < threshold:
            asked += 1
            asked_top3 += record["label"] in prediction.candidates() or record["label"] == OTHER
        elif prediction.category == OTHER:
            manual += 1
            manual_right += record["label"] == OTHER
        else:
            placed += 1
            placed_right += record["label"] == prediction.category
    total = len(results)
    print(f"{args.split}: {total} photos from {len({r['box'] for r, _ in results})} boxes, "
          f"{elapsed / max(total, 1) * 1000:.0f} ms/photo (4 threads, incl. reading)")
    print(f"top-1 accuracy {np.trace(confusion) / total:.4f}")
    names = [str(c) if c != OTHER else "6-8" for c in classes]
    print("true\\pred " + " ".join(f"{name:>5}" for name in names) + "  recall")
    for index, name in enumerate(names):
        row = confusion[index]
        print(f"{name:>9} " + " ".join(f"{value:5d}" for value in row) + f"  {row[index] / max(row.sum(), 1):.3f}")
    print(f"threshold {threshold}: placed {placed} ({placed / total:.1%}), correct {placed_right / max(placed, 1):.4f}; "
          f"asked {asked} ({asked / total:.1%}), true slot in top 3 or 6-8 {asked_top3 / max(asked, 1):.3f}; "
          f"flagged 6-8 {manual}, correct {manual_right / max(manual, 1):.4f}")
    mistakes = [
        {"path": r["path"], "label": r["label"], "pred": p.category, "conf": round(p.confidence, 3)}
        for r, p in results if p.category != r["label"] and p.confidence >= threshold
    ]
    (work / f"mistakes_{args.split}.json").write_text(json.dumps(mistakes, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("work")
    manifest.add_argument("--roots-file", required=True)
    manifest.add_argument("--holdout", action="append", default=[])
    manifest.set_defaults(run=build_manifest)
    cache = commands.add_parser("cache")
    cache.add_argument("work")
    cache.set_defaults(run=build_cache)
    fit = commands.add_parser("train")
    fit.add_argument("work")
    fit.add_argument("--epochs", type=int, default=12)
    fit.add_argument("--lr", type=float, default=1e-3)
    fit.set_defaults(run=train)
    check = commands.add_parser("evaluate")
    check.add_argument("work")
    check.add_argument("--split", default="test")
    check.add_argument("--model")
    check.add_argument("--threshold", type=float)
    check.set_defaults(run=evaluate)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
