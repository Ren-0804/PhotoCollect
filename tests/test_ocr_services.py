import unittest

from ocr_services import (
    analyze_batch_input,
    annotate_ocr_verification,
    build_container_number_hint,
    extract_declared_container_no,
    important_ocr_complete,
    iso6346_check_digit,
    merge_important_ocr_results,
    normalize_ocr_rows,
    parse_capacity,
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


def ocr_row(text, x0, y0, x1, y1, score=0.8):
    return ([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, score)


class DoorPhotoRegressionTests(unittest.TestCase):
    """Row layouts taken from real door photos that the parser used to get wrong."""

    def test_one_printed_line_straddling_a_fixed_y_bucket_is_still_joined(self):
        parsed = parse_container_ocr([
            ocr_row("DFSU", 1157, 781, 1319, 868),
            ocr_row("300257", 1383, 785, 1611, 857),
            ocr_row("25G1", 1384, 871, 1554, 959),
        ])
        self.assertEqual(parsed["container_no"], "DFSU3002572")
        self.assertEqual(parsed["size_type"], "25G1")

    def test_category_letter_read_as_zero_is_restored_to_u(self):
        for owner in ("TZI0", "TZ10"):
            with self.subTest(owner=owner):
                parsed = parse_container_ocr([
                    ocr_row(owner, 600, 274, 688, 318),
                    ocr_row("0000463", 720, 276, 880, 322),
                    ocr_row("25G1", 720, 326, 805, 374),
                    ocr_row("NET", 580, 600, 625, 625),
                    ocr_row("28.120", 650, 598, 720, 622),
                ])
                self.assertEqual(parsed["container_no"], "TZIU0000463")
                self.assertTrue(parsed["check_digit_valid"])
                self.assertFalse(parsed["inferred_check_digit"])

    def test_locking_bar_glyph_between_owner_and_serial_is_skipped(self):
        bar_before_serial = parse_container_ocr([
            ocr_row("CICU", 600, 400, 700, 445),
            ocr_row("19612195", 730, 400, 940, 445),
            ocr_row("2561", 730, 455, 820, 500),
        ])
        self.assertEqual(bar_before_serial["container_no"], "CICU9612195")
        self.assertFalse(bar_before_serial["inferred_check_digit"])

        bar_after_owner = parse_container_ocr([
            ocr_row("CICU9", 600, 400, 720, 445),
            ocr_row("960905", 740, 400, 900, 445),
            ocr_row("25G1", 740, 455, 830, 500),
        ])
        self.assertEqual(bar_after_owner["container_no"], "CICU9609057")
        self.assertTrue(bar_after_owner["inferred_check_digit"])

    def test_serial_digits_are_never_borrowed_from_the_next_line(self):
        parsed = parse_container_ocr([
            ocr_row("DFSU", 600, 400, 700, 445),
            ocr_row("30047", 730, 400, 860, 445),
            ocr_row("25G1", 730, 455, 820, 500),
        ])
        self.assertEqual(parsed["container_no"], "")

    def test_frame_edge_read_before_the_check_digit_is_ignored(self):
        parsed = parse_container_ocr([
            ocr_row("MCCU", 600, 400, 700, 445),
            ocr_row("651521", 730, 400, 890, 445),
            ocr_row("10", 905, 398, 950, 448),
        ])
        self.assertEqual(parsed["container_no"], "MCCU6515210")
        self.assertFalse(parsed["inferred_check_digit"])

    def test_tall_logo_does_not_chain_the_type_line_into_the_number_line(self):
        parsed = parse_container_ocr([
            ocr_row("LYGU", 560, 422, 627, 459),
            ocr_row("1048064", 652, 424, 769, 458),
            ocr_row("HH物流", 836, 421, 959, 482),
            ocr_row("45G1", 649, 460, 714, 502),
        ])
        self.assertEqual(parsed["container_no"], "LYGU1048064")
        self.assertFalse(parsed["inferred_check_digit"])
        self.assertEqual(parsed["size_type"], "45G1")

    def test_misread_size_type_is_corrected_only_to_a_known_code(self):
        number = [ocr_row("CICU", 600, 400, 700, 445), ocr_row("410260", 730, 400, 890, 445)]
        for text, expected in (("2561", "25G1"), ("25G1N", "25G1"), ("25C1", "25G1"), ("2.561", "")):
            with self.subTest(text=text):
                parsed = parse_container_ocr(number + [ocr_row(text, 730, 455, 820, 500)])
                self.assertEqual(parsed["size_type"], expected)

    def test_size_type_under_the_number_wins_over_a_neighbouring_door(self):
        parsed = parse_container_ocr([
            ocr_row("22G1", 20, 300, 110, 345),
            ocr_row("CICU", 600, 400, 700, 445),
            ocr_row("285247", 730, 400, 890, 445),
            ocr_row("45G1", 730, 455, 820, 500),
        ])
        self.assertEqual(parsed["size_type"], "45G1")

    def test_framed_check_digit_reader_only_confirms_the_iso_digit(self):
        rows = [
            ocr_row("CICU", 590, 407, 664, 448),
            ocr_row("410260", 694, 408, 796, 449),
            ocr_row("25G1", 697, 453, 767, 498),
        ]
        seen = []

        def reader(bounds):
            seen.append(bounds)
            return "3"

        confirmed = parse_container_ocr(rows, check_digit_reader=reader)
        self.assertEqual(confirmed["container_no"], "CICU4102603")
        self.assertFalse(confirmed["inferred_check_digit"])
        self.assertEqual(seen, [(694.0, 408.0, 796.0, 449.0)])
        self.assertEqual(
            annotate_ocr_verification(confirmed, "CICU4102603")["verification_status"], "match"
        )

        disagreeing = parse_container_ocr(rows, check_digit_reader=lambda bounds: "5")
        self.assertEqual(disagreeing["container_no"], "CICU4102603")
        self.assertTrue(disagreeing["inferred_check_digit"])

    def test_check_digit_read_on_one_door_photo_confirms_the_merged_result(self):
        inferred = {"container_no": "CICU4102603", "inferred_check_digit": True,
                    "size_type": "25G1", "max_gross_kg": 30480, "tare_kg": 2230}
        read = {"container_no": "CICU4102603", "inferred_check_digit": False, "size_type": "25G1"}

        merged = merge_important_ocr_results([inferred, read])

        self.assertFalse(merged["inferred_check_digit"])
        self.assertEqual(
            annotate_ocr_verification(merged, "CICU4102603")["verification_status"], "match"
        )


def plate(text, x0, cy, width, height=22, score=0.8):
    return ocr_row(text, x0, cy - height / 2, x0 + width, cy + height / 2, score)


def plate_values(raw):
    rows = normalize_ocr_rows(raw)
    weights = parse_weights(rows)
    capacity, _score = parse_capacity(rows)
    return weights["max_gross_kg"], weights["tare_kg"], weights["payload_kg"], capacity


# Door plate as RapidOCR reads it: each weight as a KG line with its LB line
# right below, the label between them, '.' as thousands separator, LB often
# misread as "1b", "b" or "16", then CU.M above CU.FT.
STACKED_PLATE = [
    plate("MAX GROSS", 550, 552, 100),
    plate("32.500kg", 690, 540, 80),
    plate("71.650 1b", 690, 562, 80),
    plate("TARE", 550, 623, 45),
    plate("3.600 kg", 695, 611, 75),
    plate("7.940 16", 695, 634, 75),
    plate("PAYLOAD", 550, 693, 80),
    plate("28.900kg", 690, 681, 80),
    plate("63.710 b", 690, 704, 80),
    plate("CU. CAP.", 550, 762, 70),
    plate("76.4cu.m.", 700, 750, 80),
    plate("2.700 cu.ft.", 695, 774, 90),
]


class DoorPlateWeightTests(unittest.TestCase):
    """Weights must come from the KG figures and capacity from the CU.M one."""

    def test_stacked_kg_and_lb_lines_keep_the_kg_figures(self):
        self.assertEqual(plate_values(STACKED_PLATE), (32500, 3600, 28900, 76.4))

    def test_label_number_and_unit_read_as_separate_rows(self):
        raw = [
            plate("32.500", 690, 509, 60, 28), plate("kg", 778, 510, 27, 26),
            plate("MAX.GROSS", 550, 524, 100, 30), plate("71.650", 689, 533, 56, 25),
            plate("3.700", 700, 577, 49, 29), plate("kg", 783, 579, 23, 24),
            plate("TARE", 556, 594, 43, 27), plate("8.160", 698, 602, 49, 24),
            plate("28.800", 689, 646, 58, 27), plate("kg", 776, 647, 27, 25),
            plate("PAYLOAD", 554, 661, 74, 31), plate("63.490", 692, 670, 56, 25),
            plate("76.3", 705, 712, 45), plate("CU.M.", 749, 710, 42, 23),
            plate("CU. CAP.", 555, 728, 65, 28),
            plate("2.689", 699, 736, 46), plate("CU.FT.", 749, 734, 48, 25),
        ]
        self.assertEqual(plate_values(raw), (32500, 3700, 28800, 76.3))

    def test_unit_less_pair_is_resolved_by_the_lb_conversion(self):
        raw = [
            plate("MAX GROSS", 548, 549, 85), plate("32500", 663, 541, 47),
            plate("kg", 716, 544, 21), plate("71 650", 661, 562, 44),
            plate("TARE", 549, 609, 37), plate("3600", 669, 600, 49), plate("7930", 668, 619, 37),
            plate("PAYLOAD", 547, 666, 62), plate("63720", 659, 676, 60),
        ]
        self.assertEqual(plate_values(raw)[:3], (32500, 3600, 28900))

    def test_lb_figure_is_never_stored_when_the_kg_line_was_missed(self):
        pounds_only = [
            plate("MAX GROSS", 550, 552, 100), plate("71.650 LBS", 690, 562, 90),
            plate("PAYLOAD", 550, 693, 80), plate("28.800 KGS", 690, 681, 90),
            plate("63.490 LBS", 690, 704, 90), plate("TARE", 550, 623, 45),
        ]
        cases = (
            # A lone tare figure placed like the LB lines is rejected.
            ("8.160", 634, (None, None, 28800)),
            # Placed like the KG lines it is kept; the gross then follows from
            # tare + payload because its LB line (71.650) confirms the sum.
            ("3.700", 611, (32500, 3700, 28800)),
        )
        for text, cy, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(plate_values(pounds_only + [plate(text, 695, cy, 50)])[:3], expected)

    def test_kg_and_lb_printed_on_one_line(self):
        raw = [
            plate("MAX GROSS", 100, 300, 120), plate("30,480", 260, 300, 70), plate("KGS", 340, 300, 40),
            plate("67,200", 400, 300, 70), plate("LBS", 480, 300, 40),
            plate("TARE", 100, 350, 50), plate("2,200", 260, 350, 60), plate("KGS", 340, 350, 40),
            plate("4,850", 400, 350, 60), plate("LBS", 480, 350, 40),
        ]
        self.assertEqual(plate_values(raw)[:3], (30480, 2200, 28280))

    def test_split_and_misread_figures_are_not_stored_as_fragments(self):
        split = [
            plate("MAX", 400, 300, 40), plate("GROSS", 450, 300, 60),
            plate("30.", 600, 300, 30), plate("480", 628, 300, 40), plate("KGS", 680, 300, 40),
            plate("TARE", 400, 370, 50), plate("2.230 KGS", 600, 370, 90),
            plate("PAYLOAD", 400, 440, 80), plate("390", 700, 452, 40),
        ]
        self.assertEqual(plate_values(split)[:3], (30480, 2230, 28250))

        zero_as_d = [plate("NET", 550, 665, 40), plate("28.8DD KGS", 690, 651, 90)]
        self.assertEqual(plate_values(zero_as_d)[2], 28800)

        dropped_digit = [
            plate("MAX.GROSS", 550, 552, 100), plate("30480KGS", 690, 540, 80),
            plate("67200 LBS", 690, 562, 80), plate("TARE", 550, 623, 45),
            plate("2 260 KGS", 695, 611, 75), plate("4980LBS", 695, 634, 75),
            plate("NET", 550, 693, 40), plate("2822KGS", 690, 681, 80), plate("62221LBS", 690, 704, 80),
        ]
        self.assertEqual(plate_values(dropped_digit)[:3], (30480, 2260, 28220))

    def test_kg_figures_without_labels_are_taken_in_printed_order(self):
        raw = [
            plate("MAX.GROSS", 550, 552, 100), plate("30.480 KGS", 690, 540, 90),
            plate("67.200 LBS", 690, 562, 90), plate("2.230 KGS", 690, 605, 90),
            plate("4.920LBS", 690, 627, 90), plate("28.250 KGS", 690, 670, 90),
            plate("62.280 LBS", 690, 692, 90),
        ]
        self.assertEqual(plate_values(raw)[:3], (30480, 2230, 28250))

    def test_capacity_never_comes_from_cubic_feet_or_a_weight(self):
        feet_only = [plate("CU.CAP.", 550, 700, 70), plate("2.698 CU.FT.", 690, 712, 100)]
        self.assertEqual(parse_capacity(normalize_ocr_rows(feet_only))[0], None)

        payload_lb_next_to_label = [plate("CU. CAP.", 550, 728, 65), plate("63.490", 692, 700, 56)]
        self.assertIsNone(parse_capacity(normalize_ocr_rows(payload_lb_next_to_label))[0])

        lost_decimal_point = [plate("374CU.M.", 690, 690, 90), plate("1.322CUFT.", 690, 712, 100)]
        self.assertEqual(parse_capacity(normalize_ocr_rows(lost_decimal_point))[0], 37.4)

        unit_letter_only = [plate("CUBE", 583, 717, 40), plate("76.4M", 696, 714, 65),
                            plate("2.700FT", 686, 732, 73)]
        self.assertEqual(parse_capacity(normalize_ocr_rows(unit_letter_only))[0], 76.4)

    def test_container_result_stores_kilograms_and_cubic_metres(self):
        parsed = parse_container_ocr([
            ocr_row("LYGU3043781", 560, 400, 800, 445),
            ocr_row("45G1", 600, 455, 690, 500),
            *STACKED_PLATE,
        ])
        self.assertEqual(parsed["container_no"], "LYGU3043781")
        self.assertEqual(parsed["size_type"], "45G1")
        self.assertEqual(
            (parsed["max_gross_kg"], parsed["tare_kg"], parsed["payload_kg"], parsed["capacity_m3"]),
            (32500, 3600, 28900, 76.4),
        )
        self.assertTrue(important_ocr_complete(parsed))


if __name__ == "__main__":
    unittest.main()
