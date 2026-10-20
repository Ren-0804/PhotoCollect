import unittest

from ocr_services import (
    analyze_batch_input,
    annotate_ocr_verification,
    build_container_number_hint,
    extract_declared_container_no,
    important_ocr_complete,
    iso6346_check_digit,
    merge_important_ocr_results,
    parse_container_ocr,
    parse_weights,
    validate_container_no,
)


class PureOcrServiceTests(unittest.TestCase):
    def test_container_validation_and_batch_analysis_remain_qt_independent(self):
        self.assertEqual(iso6346_check_digit("LYGU304378"), 1)
        self.assertTrue(validate_container_no("LYGU3043781"))

        analysis = analyze_batch_input(
            "LYGU 304378-1\n错误箱号\nLYGU3043781",
            set(),
        )
        self.assertEqual([item.name for item in analysis.creatable], ["LYGU3043781", "错误箱号"])
        self.assertEqual(analysis.valid_count, 1)
        self.assertEqual(analysis.warning_count, 1)
        self.assertEqual(analysis.duplicate_count, 1)

    def test_parser_and_verification_keep_existing_result_contract(self):
        raw = [
            (
                [[0, 0], [100, 0], [100, 20], [0, 20]],
                "LYGU3043781",
                0.96,
            ),
            (
                [[0, 30], [60, 30], [60, 50], [0, 50]],
                "22G1",
                0.91,
            ),
        ]

        parsed = parse_container_ocr(raw, elapsed_ms=123)
        verified = annotate_ocr_verification(parsed, "LYGU3043781", automatic=True)

        self.assertEqual(parsed["container_no"], "LYGU3043781")
        self.assertEqual(parsed["size_type"], "22G1")
        self.assertEqual(parsed["elapsed_ms"], 123)
        self.assertEqual(verified["verification_status"], "match")
        self.assertTrue(verified["automatic"])

    def test_real_split_cicu_samples_resolve_to_full_iso_numbers(self):
        cases = (
            (
                "CICU7769210",
                (("CICU", 0.7141), ("776921", 0.7963), ("25G1", 0.7551)),
            ),
            (
                "CICU7344426",
                (("CICU", 0.7232), ("734442", 0.8539), ("25G1", 0.7958)),
            ),
        )
        for expected, tokens in cases:
            raw = []
            for index, (text, score) in enumerate(tokens):
                top = index * 28
                raw.append(
                    (
                        [[0, top], [120, top], [120, top + 20], [0, top + 20]],
                        text,
                        score,
                    )
                )

            parsed = parse_container_ocr(raw)
            verified = annotate_ocr_verification(
                parsed,
                expected,
                automatic=True,
            )

            self.assertEqual(parsed["container_no"], expected)
            self.assertTrue(parsed["check_digit_valid"])
            self.assertTrue(parsed["inferred_check_digit"])
            self.assertEqual(verified["verification_status"], "inferred_match")


    def test_spatially_separated_weight_boxes_still_parse(self):
        rows = [
            {"text": "MAX GROSS", "score": 0.9, "x": 0, "y": 120},
            {"text": "30480", "score": 0.9, "x": 150, "y": 121},
            {"text": "KGS", "score": 0.9, "x": 260, "y": 119},
            {"text": "TARE", "score": 0.9, "x": 0, "y": 190},
            {"text": "2180", "score": 0.9, "x": 150, "y": 192},
            {"text": "KGS", "score": 0.9, "x": 260, "y": 189},
        ]
        weights = parse_weights(rows)
        self.assertEqual(weights["max_gross_kg"], 30480)
        self.assertEqual(weights["tare_kg"], 2180)
        self.assertEqual(weights["payload_kg"], 30480 - 2180)

    def test_inline_merged_weight_lines_are_parsed_as_fallback(self):
        rows = [
            {"text": "MAX.GROSS 30,480 KGS", "score": 0.85, "x": 0, "y": 120},
            {"text": "TARE 2.180 KGS", "score": 0.82, "x": 0, "y": 150},
        ]
        weights = parse_weights(rows)
        self.assertEqual(weights["max_gross_kg"], 30480)
        self.assertEqual(weights["tare_kg"], 2180)
        self.assertEqual(weights["payload_kg"], 30480 - 2180)

    def test_inline_fallback_never_overrides_spatial_result(self):
        rows = [
            {"text": "MAX GROSS", "score": 0.9, "x": 0, "y": 120},
            {"text": "30480", "score": 0.9, "x": 150, "y": 121},
            {"text": "KGS", "score": 0.9, "x": 260, "y": 119},
            {"text": "GROSS 99999 KG", "score": 0.4, "x": 0, "y": 400},
        ]
        weights = parse_weights(rows)
        self.assertEqual(weights["max_gross_kg"], 30480)

    def test_first_ten_characters_produce_a_live_iso6346_hint(self):
        hint = build_container_number_hint("lygu 304378")

        self.assertIsNotNone(hint)
        self.assertEqual(hint.prefix, "LYGU304378")
        self.assertEqual(hint.check_digit, 1)
        self.assertEqual(hint.complete_number, "LYGU3043781")
        self.assertIsNone(hint.provided_matches)

        wrong = build_container_number_hint("LYGU3043789")
        self.assertEqual(wrong.complete_number, "LYGU3043781")
        self.assertFalse(wrong.provided_matches)

    def test_declared_number_search_includes_the_last_possible_position(self):
        self.assertEqual(
            extract_declared_container_no("前缀-LYGU3043781"),
            "LYGU3043781",
        )

    def test_second_door_photo_is_only_needed_when_important_fields_are_missing(self):
        complete = {
            "container_no": "LYGU3043781",
            "check_digit_valid": True,
            "size_type": "22G1",
            "max_gross_kg": 30480,
            "tare_kg": 2180,
            "payload_kg": 28300,
        }
        missing_weight = {**complete, "tare_kg": None, "payload_kg": None}

        self.assertTrue(important_ocr_complete(complete))
        self.assertFalse(important_ocr_complete(missing_weight))

    def test_multiple_door_results_merge_only_missing_important_fields(self):
        first = {
            "container_no": "LYGU3043781",
            "check_digit_valid": True,
            "size_type": "22G1",
            "max_gross_kg": 30480,
            "tare_kg": None,
            "payload_kg": None,
            "elapsed_ms": 80,
            "confidence": {"container_no": 0.95},
            "raw_lines": [{"text": "LYGU3043781"}],
        }
        second = {
            "container_no": "",
            "check_digit_valid": False,
            "size_type": "",
            "max_gross_kg": 32000,
            "tare_kg": 2180,
            "payload_kg": 29820,
            "elapsed_ms": 70,
            "confidence": {"container_no": 0.2},
            "raw_lines": [{"text": "TARE 2180 KG"}],
        }

        merged = merge_important_ocr_results([first, second])

        self.assertEqual(merged["container_no"], "LYGU3043781")
        self.assertEqual(merged["size_type"], "22G1")
        self.assertEqual(merged["max_gross_kg"], 30480)
        self.assertEqual(merged["tare_kg"], 2180)
        self.assertEqual(merged["payload_kg"], 29820)
        self.assertEqual(merged["elapsed_ms"], 150)
        self.assertEqual(len(merged["raw_lines"]), 2)


if __name__ == "__main__":
    unittest.main()
