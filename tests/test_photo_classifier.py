import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import photo_classifier
from photo_classifier import (
    ASK,
    INPUT_HEIGHT,
    INPUT_WIDTH,
    MANUAL,
    OTHER,
    PLACE,
    PhotoClassifier,
    PhotoPrediction,
    load_photo,
    model_input,
    placement_for,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLED_MODELS = ROOT / "models"
FIXTURE = ROOT / "tests" / "fixtures" / "container_sample.jpg"


def jpeg_bytes(size, color, orientation=None):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    if orientation is None:
        image.save(buffer, "JPEG")
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def prediction(**probabilities):
    classes = [int(name[1:]) for name in probabilities]
    return PhotoPrediction.from_probabilities(classes, list(probabilities.values()))


class LoadPhotoTests(unittest.TestCase):
    def test_exif_orientation_is_applied(self):
        upright = load_photo(jpeg_bytes((40, 20), "red", orientation=6))

        self.assertEqual(upright.size, (20, 40))
        self.assertEqual(upright.mode, "RGB")

    def test_png_with_alpha_from_path_becomes_rgb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slot.png"
            Image.new("RGBA", (30, 40), (0, 0, 255, 128)).save(path)

            image = load_photo(str(path))

        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (30, 40))

    def test_large_jpeg_is_decoded_at_reduced_scale_but_not_below_model_input(self):
        image = load_photo(jpeg_bytes((1920, 2560), "white"))

        self.assertLess(image.size[0], 1920)
        self.assertGreaterEqual(image.size[0], INPUT_WIDTH)
        self.assertGreaterEqual(image.size[1], INPUT_HEIGHT)


class ModelInputTests(unittest.TestCase):
    def test_whole_photo_is_resized_and_normalized(self):
        batch = model_input(Image.new("RGB", (1280, 960), "white"))

        self.assertEqual(batch.shape, (1, 3, INPUT_HEIGHT, INPUT_WIDTH))
        self.assertEqual(batch.dtype, np.float32)
        expected = (1.0 - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
        np.testing.assert_allclose(batch[0, :, 10, 10], expected, rtol=1e-5)


class PredictionTests(unittest.TestCase):
    def test_ranking_is_sorted_and_candidates_skip_other(self):
        result = prediction(c3=0.2, c0=0.5, c2=0.25, c1=0.05)

        self.assertEqual(result.category, OTHER)
        self.assertAlmostEqual(result.confidence, 0.5)
        self.assertEqual([category for category, _ in result.ranking], [OTHER, 2, 3, 1])
        self.assertEqual(result.candidates(), (2, 3, 1))
        self.assertEqual(result.candidates(2), (2, 3))

    def test_placement_rules(self):
        self.assertEqual(placement_for(prediction(c4=0.95, c5=0.05), 0.9), PLACE)
        self.assertEqual(placement_for(prediction(c4=0.6, c5=0.4), 0.9), ASK)
        self.assertEqual(placement_for(prediction(c0=0.93, c3=0.07), 0.9), MANUAL)
        self.assertEqual(placement_for(prediction(c0=0.6, c3=0.4), 0.9), ASK)

    def test_softmax_sums_to_one(self):
        probabilities = photo_classifier.softmax(np.array([2.0, 1.0, -3.0, 0.5]))

        self.assertAlmostEqual(float(probabilities.sum()), 1.0)
        self.assertEqual(int(np.argmax(probabilities)), 0)


class ClassifierLoadingTests(unittest.TestCase):
    def test_missing_model_means_no_classifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(PhotoClassifier.from_directory(tmp))

    def test_config_supplies_classes_and_threshold_without_opening_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / photo_classifier.MODEL_NAME).write_bytes(b"not loaded until first prediction")
            (folder / photo_classifier.CONFIG_NAME).write_text(
                json.dumps({"classes": [5, 4, 3, 2, 1, 0], "threshold": 0.8}), encoding="utf-8"
            )

            classifier = PhotoClassifier.from_directory(folder)

        self.assertEqual(classifier.classes, (5, 4, 3, 2, 1, 0))
        self.assertEqual(classifier.threshold, 0.8)


@unittest.skipUnless((BUNDLED_MODELS / photo_classifier.MODEL_NAME).is_file(), "photo classifier model not installed")
class BundledModelTests(unittest.TestCase):
    def test_door_photo_is_classified_as_a_closed_door_slot(self):
        classifier = PhotoClassifier.from_directory(BUNDLED_MODELS)

        result = classifier.predict(str(FIXTURE))

        self.assertEqual(len(result.ranking), 6)
        self.assertAlmostEqual(sum(probability for _, probability in result.ranking), 1.0, places=4)
        self.assertIn(result.category, (4, 5))
        self.assertTrue(0.5 <= classifier.threshold < 1.0)


if __name__ == "__main__":
    unittest.main()
