"""Qt-independent container-number validation and OCR result parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


ISO6346_VALUES = {
    **{str(i): i for i in range(10)},
    "A": 10, "B": 12, "C": 13, "D": 14, "E": 15, "F": 16, "G": 17, "H": 18, "I": 19,
    "J": 20, "K": 21, "L": 23, "M": 24, "N": 25, "O": 26, "P": 27, "Q": 28, "R": 29,
    "S": 30, "T": 31, "U": 32, "V": 34, "W": 35, "X": 36, "Y": 37, "Z": 38,
}
LETTER_ZONE_FIX = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
DIGIT_ZONE_FIX = {"O": "0", "I": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}


def iso6346_check_digit(prefix10: str) -> Optional[int]:
    text = re.sub(r"[^A-Z0-9]", "", prefix10.upper())
    if len(text) != 10:
        return None
    total = 0
    for index, char in enumerate(text):
        value = ISO6346_VALUES.get(char)
        if value is None:
            return None
        total += value * (2 ** index)
    digit = total % 11
    return 0 if digit == 10 else digit


def normalize_container_candidate(text: str) -> str:
    raw = re.sub(r"[^A-Z0-9]", "", text.upper())
    if len(raw) not in (10, 11):
        return raw
    chars = list(raw)
    for index, char in enumerate(chars):
        if index < 4:
            chars[index] = LETTER_ZONE_FIX.get(char, char)
        else:
            chars[index] = DIGIT_ZONE_FIX.get(char, char)
    return "".join(chars)


@dataclass(frozen=True)
class ContainerNumberHint:
    prefix: str
    check_digit: int
    complete_number: str
    provided_matches: Optional[bool]


def build_container_number_hint(text: str) -> Optional[ContainerNumberHint]:
    """Return the ISO 6346 check digit once a valid ten-character prefix exists."""
    candidate = normalize_container_candidate(text)
    if len(candidate) not in (10, 11):
        return None
    prefix = candidate[:10]
    if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{6}", prefix):
        return None
    check_digit = iso6346_check_digit(prefix)
    if check_digit is None:
        return None
    provided_matches = None
    if len(candidate) == 11:
        provided_matches = candidate[-1] == str(check_digit)
    return ContainerNumberHint(
        prefix=prefix,
        check_digit=check_digit,
        complete_number=f"{prefix}{check_digit}",
        provided_matches=provided_matches,
    )


def validate_container_no(text: str) -> bool:
    candidate = normalize_container_candidate(text)
    if not re.fullmatch(r"[A-Z]{4}\d{7}", candidate):
        return False
    digit = iso6346_check_digit(candidate[:10])
    return digit is not None and str(digit) == candidate[-1]


def extract_declared_container_no(text: str) -> str:
    compact = re.sub(r"[^A-Z0-9]", "", str(text).upper())
    for start in range(max(1, len(compact) - 10)):
        candidate = normalize_container_candidate(compact[start:start + 11])
        if re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", candidate):
            return candidate
    return ""


def annotate_ocr_verification(
    result: dict,
    declared_name: str,
    automatic: bool = False,
) -> dict:
    enriched = dict(result or {})
    expected = extract_declared_container_no(declared_name)
    recognized = normalize_container_candidate(enriched.get("container_no", ""))
    if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", recognized):
        recognized = ""

    if not expected or not validate_container_no(expected):
        status = "expected_invalid"
    elif not recognized or not validate_container_no(recognized):
        status = "unreadable"
    elif recognized != expected:
        status = "mismatch"
    elif enriched.get("inferred_check_digit"):
        status = "inferred_match"
    else:
        status = "match"

    enriched["container_no"] = recognized
    enriched["declared_container_no"] = expected
    enriched["verification_status"] = status
    enriched["automatic"] = bool(automatic)
    enriched["recognized_at"] = datetime.now().isoformat(timespec="seconds")
    return enriched


def current_ocr_result(result, declared_name, box_dir=None):
    """Derive verification from evidence, never from a persisted status label."""
    if not isinstance(result, dict) or not result:
        return {}
    if result.get('verification_status') == 'error' and not result.get('container_no'):
        return dict(result)
    checked = annotate_ocr_verification(result, declared_name, result.get('automatic', False))
    checked['recognized_at'] = result.get('recognized_at', '')
    stale = bool(result.get('pending_source'))
    if box_dir is not None:
        from pathlib import Path
        root = Path(box_dir)
        sources = result.get('source_images') or [result.get('source_image')]
        for source in sources:
            if not source:
                continue
            try:
                path = (root / source).resolve()
                path.relative_to(root.resolve())
                info = path.stat()
                signature = result.get('source_signatures', {}).get(source)
                if signature and list(signature) != [info.st_size, info.st_mtime_ns, info.st_ctime_ns]:
                    stale = True
            except (OSError, ValueError, TypeError):
                stale = True
    if stale:
        checked.update(verification_status='unreadable', size_type='', verification_reason='照片已变化，待重新核验')
    elif result.get('conflicting_evidence'):
        checked.update(verification_status='unreadable', verification_reason='关门照片识别结果冲突，请人工核对')
    return checked


@dataclass(frozen=True)
class BatchCandidate:
    name: str
    status: str


@dataclass(frozen=True)
class BatchAnalysis:
    creatable: tuple[BatchCandidate, ...]
    valid_count: int
    warning_count: int
    duplicate_count: int
    existing_count: int


def normalize_task_name(text: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", str(text).strip()).upper()
    name = re.sub(r"[\s_-]+", "", name)
    if name in {"", ".", ".."}:
        return ""
    return normalize_container_candidate(name) if len(name) in (10, 11) else name


def analyze_batch_input(text: str, existing_names) -> BatchAnalysis:
    existing = {str(name).upper() for name in existing_names}
    seen: set[str] = set()
    creatable: list[BatchCandidate] = []
    valid_count = warning_count = duplicate_count = existing_count = 0
    for raw_line in re.split(r"[\r\n,，;；、\t]+", str(text)):
        name = normalize_task_name(raw_line)
        if not name:
            continue
        if name in seen:
            duplicate_count += 1
            continue
        seen.add(name)
        if name in existing:
            existing_count += 1
            continue
        declared = extract_declared_container_no(name)
        status = "valid" if declared and validate_container_no(declared) else "warning"
        if status == "valid":
            valid_count += 1
        else:
            warning_count += 1
        creatable.append(BatchCandidate(name=name, status=status))
    return BatchAnalysis(
        tuple(creatable),
        valid_count,
        warning_count,
        duplicate_count,
        existing_count,
    )


def classify_task_state(
    box_name: str,
    missing,
    ocr_status: str,
    has_auto_ocr_photo: bool,
    require_ocr: bool = True,
) -> str:
    declared = extract_declared_container_no(box_name)
    if not declared or not validate_container_no(declared):
        return "anomaly"
    if ocr_status in {"mismatch", "expected_invalid", "error"}:
        return "anomaly"
    if require_ocr and has_auto_ocr_photo and ocr_status in {"", "unreadable"}:
        return "anomaly"
    if missing:
        return "pending"
    return "complete"


def box_center(box):
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def normalize_ocr_rows(raw_result):
    rows = []
    for row in raw_result or []:
        try:
            box, text, score = row[0], str(row[1]), float(row[2])
            x, y = box_center(box)
            rows.append({
                "box": box,
                "text": text.strip(),
                "score": score,
                "x": x,
                "y": y,
            })
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def generate_text_sequences(rows):
    tokens = [row for row in rows if row["text"]]
    for row in tokens:
        yield row["text"], [row]

    sorted_rows = sorted(tokens, key=lambda row: (round(row["y"] / 35), row["x"]))
    for index in range(len(sorted_rows)):
        sequence = []
        for next_index in range(index, min(index + 5, len(sorted_rows))):
            if sequence and abs(sorted_rows[next_index]["y"] - sequence[-1]["y"]) > 55:
                break
            sequence.append(sorted_rows[next_index])
            yield "".join(item["text"] for item in sequence), sequence


def build_container_candidates(rows):
    candidates_by_number = {}
    for _text, parts in generate_text_sequences(rows):
        compact_parts = []
        start_offsets = []
        offset = 0
        for part in parts:
            segments = re.findall(r"[A-Z0-9]+", part["text"].upper())
            compact_part = "".join(segments)
            local_offset = 0
            for segment in segments:
                start_offsets.append(offset + local_offset)
                local_offset += len(segment)
            compact_parts.append(compact_part)
            offset += len(compact_part)

        compact = "".join(compact_parts)
        for start in dict.fromkeys(start_offsets):
            for length in (11, 10):
                chunk = compact[start:start + length]
                if len(chunk) != length:
                    continue
                if sum(char.isalpha() for char in chunk[:4]) < 3:
                    continue
                fixed = normalize_container_candidate(chunk)
                inferred = False
                valid = False
                container_no = fixed
                if re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", fixed):
                    valid = validate_container_no(fixed)
                elif re.fullmatch(r"[A-Z]{3}[UJZ]\d{6}", fixed):
                    digit = iso6346_check_digit(fixed)
                    if digit is not None:
                        container_no = f"{fixed}{digit}"
                        valid = True
                        inferred = True
                if not re.fullmatch(r"[A-Z]{3}[UJZ]\d{7}", container_no):
                    continue
                average_score = sum(part["score"] for part in parts) / max(1, len(parts))
                candidate = {
                    "container_no": container_no,
                    "valid": valid,
                    "inferred_check_digit": inferred,
                    "confidence": round(average_score, 4),
                    "source_text": " ".join(part["text"] for part in parts),
                }
                previous = candidates_by_number.get(container_no)
                quality = (not valid, inferred, -candidate["confidence"])
                if previous is not None:
                    previous_quality = (
                        not previous["valid"],
                        previous["inferred_check_digit"],
                        -previous["confidence"],
                    )
                    if quality >= previous_quality:
                        continue
                candidates_by_number[container_no] = candidate
    candidates = list(candidates_by_number.values())
    candidates.sort(key=lambda candidate: (
        not candidate["valid"],
        candidate["inferred_check_digit"],
        -candidate["confidence"],
        candidate["container_no"],
    ))
    return candidates


def parse_weight_value(text: str) -> Optional[int]:
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def parse_size_type(rows):
    for row in rows:
        text = re.sub(r"[^A-Z0-9]", "", row["text"].upper())
        if re.fullmatch(r"[1-9][0-9][A-Z][0-9]", text):
            return text, row["score"]
    return "", 0.0


def parse_capacity(rows):
    for row in rows:
        if "CU" not in row["text"].upper():
            continue
        nearby = [
            item for item in rows
            if abs(item["y"] - row["y"]) < 35 and item["x"] < row["x"]
        ]
        nearby.sort(key=lambda item: abs(item["x"] - row["x"]))
        for item in nearby:
            match = re.search(r"\d+(?:[.,]\d+)?", item["text"])
            if match:
                return (
                    float(match.group(0).replace(",", ".")),
                    min(row["score"], item["score"]),
                )
    return None, 0.0


def parse_weights(rows):
    weights = {
        "max_gross_kg": None,
        "tare_kg": None,
        "payload_kg": None,
    }
    kilograms = [row for row in rows if "KG" in row["text"].upper()]
    for unit in kilograms:
        numbers = [
            row for row in rows
            if row["x"] < unit["x"]
            and abs(row["y"] - unit["y"]) < 30
            and parse_weight_value(row["text"])
        ]
        if not numbers:
            continue
        number_row = sorted(numbers, key=lambda row: abs(row["x"] - unit["x"]))[0]
        value = parse_weight_value(number_row["text"])
        labels = " ".join(
            row["text"].upper() for row in rows
            if row["x"] < number_row["x"]
            and abs(row["y"] - number_row["y"]) < 55
        )
        if "MAX" in labels or "GROSS" in labels:
            weights["max_gross_kg"] = value
        elif "TARE" in labels or "TRE" in labels or "TAR" in labels:
            weights["tare_kg"] = value
        elif "NET" in labels or "PAYLOAD" in labels:
            weights["payload_kg"] = value
    # Fallback: some OCR engines merge a whole stencil line into one text box
    # ("MAX GROSS 30480 KGS"). The spatial matching above cannot see those, so
    # fill any still-missing weight from inline label+number+unit patterns.
    inline_patterns = (
        ("max_gross_kg", r"(?:MAX(?:IMUM)?[\s.]*)?GROSS[^0-9]*([\d.,]+)\s*(?:KGS?|KG)"),
        ("max_gross_kg", r"MAX[\s.]*(?:WT|WEIGHT)[^0-9]*([\d.,]+)\s*(?:KGS?|KG)"),
        ("tare_kg", r"TARE[^0-9]*([\d.,]+)\s*(?:KGS?|KG)"),
        ("payload_kg", r"(?:NET|PAYLOAD)[^0-9]*([\d.,]+)\s*(?:KGS?|KG)"),
    )
    for row in rows:
        text = row["text"].upper()
        if "KG" not in text:
            continue
        for key, pattern in inline_patterns:
            if weights[key] is not None:
                continue
            match = re.search(pattern, text)
            if match:
                value = parse_weight_value(match.group(1))
                if value:
                    weights[key] = value

    if (
        weights["payload_kg"] is None
        and weights["max_gross_kg"]
        and weights["tare_kg"]
    ):
        weights["payload_kg"] = (
            weights["max_gross_kg"] - weights["tare_kg"]
        )
    return weights


_IMPORTANT_OCR_FIELDS = (
    "container_no",
    "size_type",
    "max_gross_kg",
    "tare_kg",
    "payload_kg",
)


def important_ocr_complete(result: dict) -> bool:
    """Whether the operational fields requested by the workflow are all usable."""
    data = result if isinstance(result, dict) else {}
    container_no = normalize_container_candidate(data.get("container_no", ""))
    if not validate_container_no(container_no):
        return False
    if not re.fullmatch(r"[1-9][0-9][A-Z][0-9]", str(data.get("size_type", "")).upper()):
        return False
    for key in ("max_gross_kg", "tare_kg", "payload_kg"):
        try:
            if int(data.get(key) or 0) <= 0:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _ocr_result_priority(result: dict) -> tuple:
    data = result if isinstance(result, dict) else {}
    container_no = normalize_container_candidate(data.get("container_no", ""))
    valid_container = bool(
        data.get("check_digit_valid") or validate_container_no(container_no)
    )
    size_type = bool(
        re.fullmatch(r"[1-9][0-9][A-Z][0-9]", str(data.get("size_type", "")).upper())
    )
    weight_count = sum(
        1 for key in ("max_gross_kg", "tare_kg", "payload_kg")
        if data.get(key) not in (None, "", 0)
    )
    confidence = data.get("confidence", {})
    try:
        container_confidence = float(
            confidence.get("container_no", 0.0)
            if isinstance(confidence, dict)
            else 0.0
        )
    except (TypeError, ValueError, OverflowError):
        container_confidence = 0.0
    return valid_container, size_type, weight_count, container_confidence


def merge_important_ocr_results(results) -> dict:
    """Keep the best door result and fill only its missing operational fields."""
    usable = [dict(result) for result in results if isinstance(result, dict)]
    if not usable:
        return {}
    best = max(usable, key=_ocr_result_priority)
    merged = dict(best)

    numbers = {normalize_container_candidate(item.get('container_no', '')) for item in usable
               if validate_container_no(item.get('container_no', ''))}
    types = {str(item.get('size_type', '')).strip().upper() for item in usable if item.get('size_type')}
    merged['conflicting_evidence'] = len(numbers) > 1 or len(types) > 1

    for result in sorted(usable, key=_ocr_result_priority, reverse=True):
        if (result.get('container_no') and merged.get('container_no')
                and result['container_no'] != merged['container_no']):
            continue
        for key in _IMPORTANT_OCR_FIELDS:
            if merged.get(key) in (None, "", 0) and result.get(key) not in (None, "", 0):
                merged[key] = result[key]

    if (
        merged.get("payload_kg") in (None, "", 0)
        and merged.get("max_gross_kg") not in (None, "", 0)
        and merged.get("tare_kg") not in (None, "", 0)
    ):
        try:
            merged["payload_kg"] = int(merged["max_gross_kg"]) - int(merged["tare_kg"])
        except (TypeError, ValueError, OverflowError):
            pass

    merged["elapsed_ms"] = sum(
        max(0, int(result.get("elapsed_ms") or 0))
        for result in usable
        if isinstance(result.get("elapsed_ms"), (int, float))
    )
    merged["raw_lines"] = [
        line
        for result in usable
        for line in (
            result.get("raw_lines", [])
            if isinstance(result.get("raw_lines", []), list)
            else []
        )
    ]
    merged["source_images"] = list(dict.fromkeys(
        str(source)
        for result in usable
        for source in (
            result.get("source_images", [])
            if isinstance(result.get("source_images", []), list)
            else [result.get("source_image", "")]
        )
        if source
    ))
    merged["check_digit_valid"] = validate_container_no(
        merged.get("container_no", "")
    )
    for key in ('source_sha256', 'source_signatures'):
        merged[key] = {name: value for result in usable for name, value in result.get(key, {}).items()}
    return merged


def parse_container_ocr(raw_result, elapsed_ms=0, device="CPU"):
    rows = normalize_ocr_rows(raw_result)
    candidates = build_container_candidates(rows)
    best = candidates[0] if candidates else {}
    size_type, size_confidence = parse_size_type(rows)
    capacity, capacity_confidence = parse_capacity(rows)
    weights = parse_weights(rows)
    valid = bool(best.get("valid"))
    score = best.get("confidence", 0.0)
    if valid and score >= 0.78 and not best.get("inferred_check_digit"):
        level = "high"
    elif valid:
        level = "medium"
    else:
        level = "low"
    container_no = best.get("container_no", "")
    return {
        "container_no": container_no,
        "owner_code": container_no[:3] if len(container_no) == 11 else "",
        "equipment_category": container_no[3:4] if len(container_no) == 11 else "",
        "serial_number": container_no[4:10] if len(container_no) == 11 else "",
        "check_digit": container_no[10:11] if len(container_no) == 11 else "",
        "check_digit_valid": valid,
        "inferred_check_digit": bool(best.get("inferred_check_digit", False)),
        "size_type": size_type,
        **weights,
        "capacity_m3": capacity,
        "confidence": {
            "level": level,
            "container_no": round(score, 4),
            "size_type": round(size_confidence, 4),
            "capacity": round(capacity_confidence, 4),
        },
        "device": device,
        "elapsed_ms": elapsed_ms,
        "manual_corrected": False,
        "candidates": candidates[:5],
        "raw_lines": [
            {
                "text": row["text"],
                "score": round(row["score"], 4),
                "x": round(row["x"], 1),
                "y": round(row["y"], 1),
            }
            for row in rows
        ],
    }
