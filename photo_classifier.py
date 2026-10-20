"""Guess the photo slot (1-5) of a container photo with a small ONNX image classifier.

The model is trained offline by tools/train_photo_classifier.py on photos that
were already sorted into slots.  It has one extra class, OTHER, for photos that
belong to slots 6-8 (bracing, floor beams, seals); those are left to the user.
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


MODEL_NAME = "photo_classifier.onnx"
CONFIG_NAME = "photo_classifier.json"
OTHER = 0
AUTO_SORT_CATEGORIES = (1, 2, 3, 4, 5)
DEFAULT_CLASSES = (1, 2, 3, 4, 5, OTHER)
INPUT_WIDTH = 240
INPUT_HEIGHT = 320
DEFAULT_THRESHOLD = 0.9
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

PLACE = "place"
ASK = "ask"
MANUAL = "manual"


def load_photo(source: Path | str | bytes) -> Image.Image:
    """Decode a photo upright (EXIF orientation applied) as RGB.

    JPEGs are decoded at a reduced scale that is still at least twice the model
    input, which keeps a 2560 px phone photo well under 50 ms.
    """
    stream = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with Image.open(stream) as image:
        image.draft("RGB", (INPUT_WIDTH * 2, INPUT_WIDTH * 2))
        upright = ImageOps.exif_transpose(image)
        return upright.convert("RGB")


def model_input(image: Image.Image) -> np.ndarray:
    """Resize the whole photo (no crop) and normalize it to a 1x3xHxW float32 batch."""
    resized = image.convert("RGB").resize((INPUT_WIDTH, INPUT_HEIGHT), Image.BILINEAR)
    array = np.asarray(resized, dtype=np.float32) / 255.0
    array = (array - _MEAN) / _STD
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None])


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = np.asarray(logits, dtype=np.float64) - np.max(logits)
    exp = np.exp(shifted)
    return exp / exp.sum()


@dataclass(frozen=True)
class PhotoPrediction:
    category: int
    confidence: float
    ranking: tuple[tuple[int, float], ...]

    @classmethod
    def from_probabilities(cls, classes, probabilities) -> "PhotoPrediction":
        ranking = tuple(sorted(
            ((int(category), float(probability)) for category, probability in zip(classes, probabilities)),
            key=lambda item: item[1],
            reverse=True,
        ))
        return cls(category=ranking[0][0], confidence=ranking[0][1], ranking=ranking)

    def candidates(self, limit: int = 3) -> tuple[int, ...]:
        """Most likely slots 1-5 first, for the "which slot?" prompt."""
        return tuple(category for category, _ in self.ranking if category != OTHER)[:limit]


def placement_for(prediction: PhotoPrediction, threshold: float) -> str:
    """PLACE into prediction.category, ASK the user, or leave it for MANUAL sorting."""
    if prediction.confidence < threshold:
        return ASK
    return MANUAL if prediction.category == OTHER else PLACE


class PhotoClassifier:
    """Thread-safe wrapper around the ONNX model; the session is created on first use."""

    def __init__(self, model_path: Path | str, *, classes=DEFAULT_CLASSES, threshold: float = DEFAULT_THRESHOLD):
        self.model_path = Path(model_path)
        self.classes = tuple(int(category) for category in classes)
        self.threshold = float(threshold)
        self._session = None
        self._lock = threading.Lock()

    @classmethod
    def from_directory(cls, directory: Path | str) -> "PhotoClassifier | None":
        """Load model + config from a folder, or None when the model is not installed."""
        directory = Path(directory)
        model_path = directory / MODEL_NAME
        if not model_path.is_file():
            return None
        config = {}
        config_path = directory / CONFIG_NAME
        if config_path.is_file():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        return cls(
            model_path,
            classes=config.get("classes", DEFAULT_CLASSES),
            threshold=config.get("threshold", DEFAULT_THRESHOLD),
        )

    def _ensure_session(self):
        with self._lock:
            if self._session is None:
                import onnxruntime

                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = 2
                self._session = onnxruntime.InferenceSession(
                    str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
                )
            return self._session

    def predict_image(self, image: Image.Image) -> PhotoPrediction:
        session = self._ensure_session()
        name = session.get_inputs()[0].name
        logits = session.run(None, {name: model_input(image)})[0][0]
        return PhotoPrediction.from_probabilities(self.classes, softmax(logits))

    def predict(self, source: Path | str | bytes) -> PhotoPrediction:
        return self.predict_image(load_photo(source))
