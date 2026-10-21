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


# The equipment-category letter (4th character) is U for freight containers;
# OCR often returns a round glyph for it ("TZI0", "TZ10").
CATEGORY_FIX = {"0": "U", "O": "U", "D": "U", "Q": "U"}
_DEFAULT_ROW_HEIGHT = 40.0


def _row_bounds(row):
    """Return (x0, y0, x1, y1) for an OCR row, estimating it when no box exists."""
    box = row.get("box")
    if box:
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            return min(xs), min(ys), max(xs), max(ys)
        except (TypeError, ValueError, IndexError):
            pass
    half_h = _DEFAULT_ROW_HEIGHT / 2
    width = max(1, len(row.get("text", ""))) * _DEFAULT_ROW_HEIGHT / 2
    x, y = float(row["x"]), float(row["y"])
    return x - width / 2, y - half_h, x + width / 2, y + half_h


def group_rows_into_lines(rows):
    """Group OCR rows into text lines, each sorted left to right.

    Fixed-size y buckets split a line whenever its rows straddle a bucket edge,
    so a row joins a line when its centre is within half a text height of a
    member's centre. Plain overlap is not enough: a tall logo block overlaps
    two printed lines and would chain them together.
    """
    tokens = []
    for row in rows:
        if not row.get("text"):
            continue
        x0, y0, x1, y1 = _row_bounds(row)
        tokens.append({**row, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "h": max(1.0, y1 - y0)})
    tokens.sort(key=lambda token: (token["y0"] + token["y1"]) / 2)
    lines = []
    for token in tokens:
        best_line, best_distance = None, None
        center = (token["y0"] + token["y1"]) / 2
        for line in lines:
            for member in line:
                distance = abs(center - (member["y0"] + member["y1"]) / 2)
                distance /= min(token["h"], member["h"])
                if distance <= 0.5 and (best_distance is None or distance < best_distance):
                    best_line, best_distance = line, distance
        if best_line is None:
            lines.append([token])
        else:
            best_line.append(token)
    lines.sort(key=lambda line: sum((t["y0"] + t["y1"]) / 2 for t in line) / len(line))
    return [sorted(line, key=lambda token: token["x0"]) for line in lines]


def _fix_owner_code(raw):
    """Map a 4-character OCR chunk to an owner code + category, or None."""
    if len(raw) != 4:
        return None, 0
    # Three letters as read, or two leading ones when U also misread ("TZ10").
    if sum(char.isalpha() for char in raw) < 3 and not (raw[0].isalpha() and raw[1].isalpha()):
        return None, 0
    chars = [LETTER_ZONE_FIX.get(char, char) for char in raw[:3]]
    category = raw[3] if raw[3] in "UJZ" else CATEGORY_FIX.get(raw[3], "")
    owner = "".join(chars) + category
    if not re.fullmatch(r"[A-Z]{3}[UJZ]", owner):
        return None, 0
    return owner, sum(a != b for a, b in zip(owner, raw))


def _segment_chars(text):
    """Alphanumeric characters of a row with a flag marking each segment start."""
    chars = []
    for match in re.finditer(r"[A-Z0-9]+", text.upper()):
        chars.extend((char, index == 0) for index, char in enumerate(match.group(0)))
    return chars


def _digit_stream(lines, line_index, token_index, offset, height):
    """Characters following an owner code as (char, line, token, boundary, gap).

    ``boundary`` marks the first character of a segment or token and ``gap``
    is the horizontal gap before a new token in multiples of the text height.
    Same-line tokens are followed while the gap stays plausible; the next line
    is only a fallback for layouts that wrap the serial.
    """
    line = lines[line_index]
    token = line[token_index]
    stream = [
        (char, line_index, token_index, boundary, 0.0)
        for char, boundary in _segment_chars(token["text"])[offset:]
    ]
    previous = token
    for next_index in range(token_index + 1, min(len(line), token_index + 4)):
        candidate = line[next_index]
        gap = (candidate["x0"] - previous["x1"]) / height
        if gap > 6:
            break
        for position, (char, _boundary) in enumerate(_segment_chars(candidate["text"])):
            stream.append((char, line_index, next_index, position == 0, gap if position == 0 else 0.0))
        previous = candidate
    if line_index + 1 < len(lines):
        below = [
            (index, item) for index, item in enumerate(lines[line_index + 1])
            if token["x0"] - 2 * height <= item["x0"] <= token["x1"] + 8 * height
        ]
        for index, item in below[:2]:
            for position, (char, _boundary) in enumerate(_segment_chars(item["text"])):
                stream.append((char, line_index + 1, index, position == 0, 0.0))
    return stream


_BOX_EDGE_GLYPHS = "1IL"


def _check_readings(stream, index, last):
    """Possible check digits read after the serial, plain reading first.

    The digit only counts when it sits on the serial's line, in the same row
    or in the next row close by. The frame printed around the check digit is
    often read as a leading "1"/"I", so "10" also offers "0".
    """
    if len(stream) <= index:
        return []
    char, line_no, token_no, boundary, gap = stream[index]
    same_row = token_no == last[2]
    next_row = token_no == last[2] + 1 and boundary and gap <= 2.5
    if line_no != last[1] or not (same_row or next_row):
        return []
    rest = [item[0] for item in stream[index:] if item[1] == line_no and item[2] == token_no]
    readings = [DIGIT_ZONE_FIX.get(char, char)]
    if len(rest) >= 2 and rest[0] in _BOX_EDGE_GLYPHS and (len(rest) == 2 or rest[2] in _BOX_EDGE_GLYPHS):
        readings.append(DIGIT_ZONE_FIX.get(rest[1], rest[1]))
    return [(digit, (line_no, token_no)) for digit in dict.fromkeys(readings) if digit.isdigit()]


def _read_serial(stream, skip):
    """Return (serial, check_readings, used_tokens, fixes, serial_row_ref) or None."""
    start = 1 if skip else 0
    chars = stream[start:start + 6]
    if len(chars) != 6:
        return None
    serial = "".join(DIGIT_ZONE_FIX.get(item[0], item[0]) for item in chars)
    if not serial.isdigit():
        return None
    fixes = sum(DIGIT_ZONE_FIX.get(item[0], item[0]) != item[0] for item in chars)
    # A serial is printed on one line; never splice digits from two lines.
    if len({item[1] for item in chars}) > 1:
        return None
    if skip and (stream[0][1] != chars[0][1] or len({item[2] for item in chars}) > 1):
        return None  # a skipped glyph must leave one clean serial row
    used = {(item[1], item[2]) for item in chars}
    serial_ref = (chars[-1][1], chars[-1][2])
    return serial, _check_readings(stream, start + 6, chars[-1]), used, fixes, serial_ref


def _container_candidates(rows):
    """Candidates with their source rows; best first.

    Ranking: a check digit read on the serial's line that validates beats an
    inferred one, which beats an inferred one whose read digit disagreed;
    structural penalties (skipped stray glyph, wrapped line, glyph fixes)
    break ties before OCR confidence.
    """
    lines = group_rows_into_lines(rows)
    best = {}
    for line_index, line in enumerate(lines):
        for token_index, token in enumerate(line):
            token_chars = _segment_chars(token["text"])
            compact = "".join(char for char, _boundary in token_chars)
            starts = [index for index, (_char, boundary) in enumerate(token_chars) if boundary]
            for start in starts:
                owner, owner_fixes = _fix_owner_code(compact[start:start + 4])
                if owner is None:
                    continue
                stream = _digit_stream(lines, line_index, token_index, start + 4, token["h"])
                for skip in (False, True):
                    if skip:
                        if len(stream) < 7:
                            continue
                        # A locking bar between owner code and serial is often
                        # read as one extra glyph right at that boundary.
                        stray, following = stream[0], stream[1]
                        at_owner_end = (
                            stray[2] == token_index and not stray[3] and following[3]
                        )
                        at_next_start = stray[2] != token_index and stray[3]
                        if not (at_owner_end or at_next_start):
                            continue
                    parsed = _read_serial(stream, skip)
                    if parsed is None:
                        continue
                    serial, checks, used, fixes, serial_ref = parsed
                    prefix = owner + serial
                    computed = iso6346_check_digit(prefix)
                    if computed is None:
                        continue
                    check, tier, edge_strip = "", 1, 0
                    for reading_index, (digit, token_ref) in enumerate(checks):
                        if digit == str(computed):
                            check, tier, edge_strip = digit, 0, reading_index
                            used.add(token_ref)
                            break
                    else:
                        if checks:
                            check, tier = checks[0][0], 2
                            used.add(checks[0][1])
                    if skip and tier != 0:
                        # Without a confirming check digit, only accept the
                        # skip when it leaves a clean six-digit serial row.
                        serial_token = lines[stream[1][1]][stream[1][2]]
                        if len(_segment_chars(serial_token["text"])) != 6:
                            continue
                    used.add((line_index, token_index))
                    parts = [lines[li][ti] for li, ti in sorted(used)]
                    crosses = serial_ref[0] != line_index
                    penalty = int(skip) + int(crosses) + edge_strip + owner_fixes + fixes
                    confidence = sum(part["score"] for part in parts) / len(parts)
                    entries = [(f"{prefix}{computed}", tier, True)]
                    if tier == 2:
                        entries.append((f"{prefix}{check}", 3, False))
                    for number, entry_tier, valid in entries:
                        key = (entry_tier, penalty, -round(confidence, 4), number)
                        previous = best.get(number)
                        if previous is not None and previous[0] <= key:
                            continue
                        best[number] = (key, {
                            "container_no": number,
                            "valid": valid,
                            "inferred_check_digit": entry_tier in (1, 2),
                            "confidence": round(confidence, 4),
                            "source_text": " ".join(part["text"] for part in parts),
                        }, parts, lines[serial_ref[0]][serial_ref[1]])
    ranked = sorted(best.values(), key=lambda item: item[0])
    return [(candidate, parts, serial_row) for _key, candidate, parts, serial_row in ranked]


def build_container_candidates(rows):
    return [candidate for candidate, *_rest in _container_candidates(rows)]


def _frame_interiors(gray, height):
    """Boxes (x, y, w, h) inside frames that could enclose a check digit.

    Two sources: the frame's hole itself, and the frame's outline inset by
    its line width, for a "1" whose stroke touches the frame and splits the
    hole in two.
    """
    import cv2

    interiors = []
    # Global Otsu works on clean doors; a dark door gap or a bright sky in the
    # crop dominates it, so a local threshold is tried as well.
    block = max(3, int(0.6 * height) | 1)
    binaries = []
    for polarity in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        binaries.append(cv2.threshold(gray, 0, 255, polarity | cv2.THRESH_OTSU)[1])
        binaries.append(cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, polarity, block, -5 if polarity == cv2.THRESH_BINARY else 5,
        ))
    for binary in binaries:
        contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            continue
        for index, contour in enumerate(contours):
            x, y, w, h = cv2.boundingRect(contour)
            parent, first_child = hierarchy[0][index][3], hierarchy[0][index][2]
            if parent >= 0:
                if not (0.7 * height <= h <= 1.8 * height and 0.3 * height <= w <= 1.4 * height):
                    continue
                if cv2.contourArea(contour) >= 0.6 * w * h:
                    inset = max(1, int(round(0.12 * min(w, h))))
                    interiors.append((x + inset, y + inset, w - 2 * inset, h - 2 * inset))
            elif first_child >= 0:
                if not (0.9 * height <= h <= 2.0 * height and 0.4 * height <= w <= 1.6 * height):
                    continue
                if not 1.0 <= h / float(w) <= 3.0:
                    continue
                dx, dy = max(2, int(round(0.15 * w))), max(2, int(round(0.1 * h)))
                interiors.append((x + dx, y + dy, w - 2 * dx, h - 2 * dy))
    return interiors


_CHECK_GLYPH_FIX = {**DIGIT_ZONE_FIX, "L": "1", "T": "1", "J": "1", "|": "1", "!": "1",
                    "D": "0", "Q": "0"}


def read_boxed_check_digit(engine, image, serial_bounds) -> Optional[str]:
    """Read the framed check digit printed right of the serial, or None.

    The recognition model reads a digit inside its frame as a CJK glyph
    (口/日/国) or drops it, so the frame's inner hole is cut out and
    recognized alone. ``image`` is the array the engine recognized and
    ``serial_bounds`` the (x0, y0, x1, y1) of the serial's text row.
    """
    import cv2
    import numpy as np

    x0, y0, x1, y1 = (float(value) for value in serial_bounds)
    height = max(1.0, y1 - y0)
    image_h, image_w = image.shape[:2]
    top, bottom = int(max(0, y0 - 0.5 * height)), int(min(image_h, y1 + 0.5 * height))
    left, right = int(max(0, x1 - 0.3 * height)), int(min(image_w, x1 + 2.5 * height))
    if bottom - top < 8 or right - left < 8:
        return None
    crop = image[top:bottom, left:right]
    # Thin frame lines break apart when binarized at phone-photo scale.
    scale = max(1.0, 64.0 / height)
    if scale > 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    readings = {}
    for x, y, w, h in _frame_interiors(gray, height * scale):
        inner = crop[y:y + h, x:x + w]
        if inner.shape[0] < 6 or inner.shape[1] < 4:
            continue
        # Strokes cover well under half of the hole, so the median is paint.
        pixels = inner.reshape(-1, inner.shape[2]) if inner.ndim == 3 else inner.reshape(-1)
        fill = np.median(pixels, axis=0).tolist() if inner.ndim == 3 else float(np.median(pixels))
        pad = max(2, int(0.3 * inner.shape[0]))
        padded = cv2.copyMakeBorder(inner, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=fill)
        result, _elapsed = engine.text_recognizer([padded])
        text, score = result[0]
        text = re.sub(r"[^0-9A-Z|!]", "", str(text).upper())
        if len(text) == 1:
            digit = _CHECK_GLYPH_FIX.get(text, text)
            if digit.isdigit() and float(score) >= 0.2:
                readings[digit] = max(readings.get(digit, 0.0), float(score))
    if not readings:
        return None
    return max(readings, key=readings.get)


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


SIZE_TYPE_PATTERN = r"[1-9][0-9][A-Z][0-9]"
# Codes a misread token may be corrected to; exact reads need not be listed.
COMMON_SIZE_TYPES = frozenset(
    [f"22G{d}" for d in "012349"] + [f"42G{d}" for d in "0123"] + [f"45G{d}" for d in "01"]
    + ["20G0", "20G1", "40G0", "40G1", "25G0", "25G1"]
    + ["22R1", "22R9", "25R1", "42R1", "45R1", "45R9", "22H0", "42H0"]
    + ["22U1", "22U6", "42U1", "42U6", "45U1", "22P1", "22P3", "42P1", "42P3", "45P3"]
    + ["22V0", "22B0", "22T0", "22T6", "42T0"]
)
_SIZE_TYPE_POSITION_FIX = (
    {"Z": "2", "S": "5", "A": "4"},
    {"O": "0", "D": "0", "Z": "2", "S": "5", "B": "8"},
    {"6": "G", "C": "G", "O": "G", "Q": "G"},
    {"I": "1", "L": "1", "T": "1", "J": "1", "O": "0", "D": "0"},
)


ISO_TYPE_GROUPS = frozenset("GVBSRHUPTA")


def _size_type_reading(text: str):
    """Return (code, corrected) for a row that holds a size-type code, else None."""
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    if re.fullmatch(SIZE_TYPE_PATTERN, compact) and compact[2] in ISO_TYPE_GROUPS:
        return compact, False
    if "." in text or "," in text:
        return None  # weights and capacities, never a type code
    if len(compact) == 5 and re.fullmatch(SIZE_TYPE_PATTERN, compact[:4]) and compact[4].isalpha():
        return compact[:4], True
    if len(compact) == 4:
        fixed = "".join(
            table.get(char, char) for table, char in zip(_SIZE_TYPE_POSITION_FIX, compact)
        )
        if fixed in COMMON_SIZE_TYPES:
            return fixed, True
    return None


def parse_size_type(rows, anchor=None):
    """Pick the size-type code, preferring the one printed under the number.

    ``anchor`` is (x0, y0, x1, y1, height) of the recognized container number.
    """
    found = []
    for index, row in enumerate(rows):
        reading = _size_type_reading(row["text"])
        if reading is None:
            continue
        code, corrected = reading
        in_zone = False
        if anchor is not None:
            ax0, ay0, ax1, ay1, height = anchor
            x0, y0, x1, y1 = _row_bounds(row)
            center_y = (y0 + y1) / 2
            below = (
                ay1 - 0.3 * height <= center_y <= ay1 + 4 * height
                and x1 >= ax0 - height and x0 <= ax1 + height
            )
            beside = abs(center_y - (ay0 + ay1) / 2) < 0.6 * height and x0 >= ax1 - 0.5 * height
            in_zone = below or beside
        found.append(((not in_zone, corrected, index), code, row["score"]))
    if not found:
        return "", 0.0
    _key, code, score = min(found, key=lambda item: item[0])
    return code, score


# Door plates print every weight twice, the KG line with the LB line right
# below it (or beside it), and the capacity as CU.M then CU.FT. Labels sit left
# of the figures, often as separate rows, and '.' is the thousands separator.
# Printed conversions agree within 0.3%; a misread digit is usually further off.
LB_PER_KG = 2.20462
CUFT_PER_M3 = 35.3147
_KG_RANGE = (1000, 40000)  # an LB gross figure is above it, fragments below it
_M3_RANGE = (5.0, 150.0)  # a CU.FT figure is outside it however '.' is read
# Checked in order: "MAX. PAYLOAD" / "MAX CARGO" are payload, a bare "MAX" is gross.
_QUANTITY_LABELS = (
    ("payload_kg", re.compile(r"\bPA[YVW]|LOAD|CARGO|\bNETT?\b")),
    ("tare_kg", re.compile(r"TARE|\bTAR\b|\bTRE\b|^\W*ARE\W*$")),
    ("max_gross_kg", re.compile(r"GROSS|\bGRO\b|M\.?\s*G\.?\s*W|\bMAX")),
    ("capacity_m3", re.compile(r"\bCU\b[.\s]*CA|CUBE|CAPA|\bCAP\b")),
)
_WEIGHT_FIELDS = ("max_gross_kg", "tare_kg", "payload_kg")
# Unit text after a number: "kg", "K", "LBS", "1b"/"b"/"16" (misread lb),
# "CU.M.", "M", "CU.FT.", "cu.t.", "CU.FL.".
_UNIT_AFTER_NUMBER = (
    ("kg", re.compile(r"K")),
    ("lb", re.compile(r"LB|[1I]?B|16")),
    ("ft3", re.compile(r"(?:CU?)?[FT]")),
    ("m3", re.compile(r"(?:CU|CB)?M|CU")),
)
# A row holding only a unit, printed right of its number.
_UNIT_ONLY = (
    ("kg", re.compile(r"K[GC6]?S?")),
    ("lb", re.compile(r"[L1I]?BS?")),
    ("ft3", re.compile(r"(?:CU)?F[TL]?|CUT")),
    ("m3", re.compile(r"(?:CU|CB)?M3?")),
)
_ZERO_GLYPH = re.compile(r"(?<=\d)[OD]|(?<=\d[.,])[OD]")
# Space-separated groups count only as thousands ("71 650"), so "63.710 16"
# keeps "16" (a misread lb) as its unit; "28-130" is a misread "28.130".
_QUANTITY_NUMBER = re.compile(r"\d+(?:[.,]\d+|-\d{3}(?!\d))*(?:\s\d{3}(?!\d))*")


def _split_quantity(text):
    """Return (label part, number, unit part) of an OCR row, number '' if none."""
    upper = text.upper()
    fixed = None
    while fixed != upper:
        fixed, upper = upper, _ZERO_GLYPH.sub("0", upper)  # "28.8DD" -> "28.800"
    match = _QUANTITY_NUMBER.search(upper)
    if not match:
        return upper, "", ""
    return upper[:match.start()], match.group(0), upper[match.end():]


def _quantity_label(text):
    for field, pattern in _QUANTITY_LABELS:
        if pattern.search(text):
            return field
    return None


def _unit_kind(text, table, whole):
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    for kind, pattern in table:
        if (pattern.fullmatch if whole else pattern.match)(compact):
            return kind
    return None


def _m3_value(number):
    # Cubic metres carry at most two decimals; "2.698" is a CU.FT or KG figure.
    match = re.fullmatch(r"(\d+)(?:[.,](\d{1,2}))?", number.replace(" ", ""))
    if not match:
        return None
    return float(f"{match.group(1)}.{match.group(2) or 0}")


def _same_line(a, b):
    return abs(a["cy"] - b["cy"]) <= 0.5 * min(a["h"], b["h"])


def _printed_before(a, b):
    if _same_line(a, b):
        return a["x0"] < b["x0"]
    return a["cy"] < b["cy"]


def _pounds_confirm(readings, kilograms):
    return any(
        r["unit"] in ("lb", None) and abs(r["value"] / kilograms / LB_PER_KG - 1) <= 0.006
        for r in readings
    )


def _join_split_numbers(readings):
    """Join a figure read as two rows ("30." then "480") back into one."""
    for reading in readings:
        if reading.get("joined") or reading["tail"] not in (".", ","):
            continue
        for other in readings:
            if (
                other is not reading and not other.get("joined") and not other["own_label"]
                and re.fullmatch(r"\d{3}", other["number"]) and _same_line(reading, other)
                and -0.5 * reading["h"] <= other["x0"] - reading["x1"] <= 0.6 * reading["h"]
            ):
                number = f"{reading['number']}.{other['number']}"
                reading.update(number=number, value=int(re.sub(r"\D", "", number)),
                               x1=other["x1"], unit=other["unit"], tail=other["tail"],
                               score=min(reading["score"], other["score"]))
                other["joined"] = True
                break
    return [reading for reading in readings if not reading.get("joined")]


def _quantity_readings(rows):
    """Number rows of the weight/capacity block, with unit and label field.

    Each reading carries ``unit`` (kg, lb, m3, ft3 or None), ``field`` (the
    label it belongs to, or None), ``offset`` (its centre below the label's
    centre, in text heights) and ``cost`` (abs offset, None without a label).
    """
    labels, readings, units = [], [], []
    for row in rows:
        if not row.get("text"):
            continue
        x0, y0, x1, y1 = _row_bounds(row)
        token = {"row": row, "x0": x0, "x1": x1, "xc": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                 "h": max(1.0, y1 - y0), "score": float(row.get("score", 0.0))}
        label_text, number, unit_text = _split_quantity(row["text"])
        field = _quantity_label(label_text)
        if field:
            labels.append((field, token))
        if number:
            token.update(number=number, value=int(re.sub(r"\D", "", number)),
                         unit=_unit_kind(unit_text, _UNIT_AFTER_NUMBER, whole=False),
                         tail=unit_text.strip(), own_label=field)
            readings.append(token)
        elif field is None:
            kind = _unit_kind(label_text, _UNIT_ONLY, whole=True)
            if kind:
                units.append((kind, token))
    readings = _join_split_numbers(readings)
    for kind, unit in units:
        left = [
            reading for reading in readings
            if _same_line(reading, unit) and reading["xc"] < unit["xc"]
            and unit["x0"] - reading["x1"] <= 3 * unit["h"]
        ]
        if left:
            reading = min(left, key=lambda item: (
                abs(item["cy"] - unit["cy"]) > 0.25 * unit["h"], unit["x0"] - item["x1"]))
            if reading["unit"] is None:
                reading["unit"] = kind
                reading["score"] = min(reading["score"], unit["score"])
    for reading in readings:
        reading["field"], reading["offset"], reading["cost"] = None, 0.0, None
        if reading["own_label"]:
            reading["field"], reading["cost"] = reading["own_label"], 0.0
            continue
        if reading["unit"] in ("kg", "lb"):
            allowed = _WEIGHT_FIELDS
        elif reading["unit"] in ("m3", "ft3"):
            allowed = ("capacity_m3",)
        else:
            allowed = None
        best = None
        for field, label in labels:
            if allowed is not None and field not in allowed:
                continue
            if label["xc"] >= reading["x0"]:
                continue  # labels are printed left of their figures
            offset = (reading["cy"] - label["cy"]) / reading["h"]
            if abs(offset) > 1.5:
                continue
            key = (abs(offset), reading["x0"] - label["x1"])
            if best is None or key < best[0]:
                best = (key, field, offset)
        if best is not None:
            _key, reading["field"], reading["offset"] = best
            reading["cost"] = abs(reading["offset"])
        elif reading["unit"] in ("m3", "ft3"):
            reading["field"] = "capacity_m3"
    return readings


def _pick_kilograms(readings, has_lb, offsets):
    """Pick the KG figure among the readings of one weight label, or None."""
    plausible = [r for r in readings if _KG_RANGE[0] <= r["value"] <= _KG_RANGE[1]]
    tagged = [r for r in plausible if r["unit"] == "kg"]
    if tagged:
        return min(tagged, key=lambda r: (max(r["cost"], 0.6), -r["score"]))
    unknown = [r for r in plausible if r["unit"] is None]
    paired = [r for r in unknown if _pounds_confirm([p for p in readings if p is not r], r["value"])]
    if paired:
        return min(paired, key=lambda r: r["cost"])
    pounds = [r for r in readings if r["unit"] == "lb"]
    if pounds:
        # The KG line is printed before (above or left of) its LB line.
        before = [r for r in unknown if any(_printed_before(r, lb) for lb in pounds)]
        return min(before, key=lambda r: r["cost"]) if before else None
    if len(unknown) > 1:
        first = unknown[0]
        for reading in unknown[1:]:
            if _printed_before(reading, first):
                first = reading
        return first
    if len(unknown) == 1:
        # A lone figure on a plate that also prints pounds may be either line;
        # keep it only when its place beside the label matches the KG lines.
        reading = unknown[0]
        if not has_lb:
            return reading
        kg_offsets, lb_offsets = offsets
        if kg_offsets and lb_offsets:
            kg_mid = sorted(kg_offsets)[len(kg_offsets) // 2]
            lb_mid = sorted(lb_offsets)[len(lb_offsets) // 2]
            if (abs(kg_mid - lb_mid) >= 0.5
                    and abs(reading["offset"] - kg_mid) < abs(reading["offset"] - lb_mid)):
                return reading
    return None


def parse_capacity(rows):
    """Return (cubic metres, confidence) from the CU.M figure, never the CU.FT one."""
    readings = [r for r in _quantity_readings(rows) if r["field"] == "capacity_m3"]
    feet = [r for r in readings if r["unit"] in ("ft3", None)]
    choices = []
    for reading in readings:
        m3 = _m3_value(reading["number"]) if reading["unit"] in ("m3", None) else None
        if m3 is None:
            continue
        options = [(m3, False)]
        if not re.search(r"[.,]", reading["number"]):
            options.append((m3 / 10, True))  # decimal point lost: "374CU.M." for 37.4
        for value, needs_feet in options:
            if not _M3_RANGE[0] <= value <= _M3_RANGE[1]:
                continue
            confirmed = any(
                p is not reading and abs(p["value"] / value / CUFT_PER_M3 - 1) <= 0.01 for p in feet
            )
            if needs_feet and not confirmed:
                continue
            rank = 0 if reading["unit"] == "m3" else (1 if confirmed else 2)
            key = (rank, reading["cost"] is None, reading["cost"] or 0.0, -reading["score"])
            choices.append((key, round(value, 2), reading["score"]))
            break
    if not choices:
        return None, 0.0
    _key, value, score = min(choices, key=lambda choice: choice[0])
    return value, score


def parse_weights(rows):
    """Return kilogram weights read from the KG figures, never the LB ones."""
    weights = {
        "max_gross_kg": None,
        "tare_kg": None,
        "payload_kg": None,
    }
    all_readings = _quantity_readings(rows)
    readings = [r for r in all_readings if r["field"] in _WEIGHT_FIELDS]
    by_field = {field: [r for r in readings if r["field"] == field] for field in _WEIGHT_FIELDS}
    has_lb = any(r["unit"] == "lb" for r in readings) or any(
        a["value"] and _pounds_confirm([b for b in group if b is not a], a["value"])
        for group in by_field.values() for a in group
    )
    offsets = (
        [r["offset"] for r in readings if r["unit"] == "kg" and not r["own_label"]],
        [r["offset"] for r in readings if r["unit"] == "lb" and not r["own_label"]],
    )
    for field in _WEIGHT_FIELDS:
        picked = _pick_kilograms(by_field[field], has_lb, offsets)
        if picked is not None:
            weights[field] = picked["value"]

    def consistent(gross, tare, payload):
        return abs(gross - tare - payload) <= max(20, 0.01 * gross)

    # Labels the OCR missed: three KG figures that add up are gross, tare and
    # payload from top to bottom.
    kilograms = sorted(
        (r for r in all_readings if r["unit"] == "kg" and _KG_RANGE[0] <= r["value"] <= _KG_RANGE[1]),
        key=lambda r: r["cy"],
    )
    if None in weights.values() and len(kilograms) == 3:
        ordered = [r["value"] for r in kilograms]
        if consistent(*ordered) and all(
            weights[field] in (None, value) for field, value in zip(_WEIGHT_FIELDS, ordered)
        ):
            weights.update(zip(_WEIGHT_FIELDS, ordered))

    # A KG figure the OCR missed or cut short ("2822" for 28220) is restored
    # from the other two only when its own LB line confirms the result; a
    # full-length figure stays as printed even when the plate does not add up.
    gross, tare, payload = (weights[field] for field in _WEIGHT_FIELDS)
    derived = {
        "max_gross_kg": tare + payload if tare and payload else None,
        "tare_kg": gross - payload if gross and payload else None,
        "payload_kg": gross - tare if gross and tare else None,
    }
    agree = bool(gross and tare and payload and consistent(gross, tare, payload))
    for field in _WEIGHT_FIELDS:
        value, current = derived[field], weights[field]
        if not value or value <= 0 or agree:
            continue
        if current is not None and len(str(current)) >= len(str(value)):
            continue
        if _pounds_confirm(by_field[field], value):
            weights[field] = value
            break

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
    # Another door photo that read the same number's check digit confirms it.
    if merged.get("inferred_check_digit") and any(
        result.get("container_no") == merged.get("container_no")
        and not result.get("inferred_check_digit")
        and validate_container_no(result.get("container_no", ""))
        for result in usable
    ):
        merged["inferred_check_digit"] = False
    for key in ('source_sha256', 'source_signatures'):
        merged[key] = {name: value for result in usable for name, value in result.get(key, {}).items()}
    return merged


def parse_container_ocr(raw_result, elapsed_ms=0, device="CPU", check_digit_reader=None):
    """Parse raw OCR rows into the container result.

    ``check_digit_reader(serial_bounds)`` may re-read the framed check digit
    from the image when the text pass only allowed inferring it; the read
    digit upgrades the result only when it equals the ISO 6346 digit.
    """
    rows = normalize_ocr_rows(raw_result)
    ranked = _container_candidates(rows)
    candidates = [candidate for candidate, *_rest in ranked]
    best = candidates[0] if candidates else {}
    if best.get("inferred_check_digit") and check_digit_reader is not None:
        serial_row = ranked[0][2]
        digit = check_digit_reader(
            (serial_row["x0"], serial_row["y0"], serial_row["x1"], serial_row["y1"])
        )
        if digit is not None and str(digit) == best["container_no"][-1]:
            best["inferred_check_digit"] = False
    anchor = None
    if ranked:
        parts = ranked[0][1]
        heights = sorted(part["h"] for part in parts)
        anchor = (
            min(part["x0"] for part in parts),
            min(part["y0"] for part in parts),
            max(part["x1"] for part in parts),
            max(part["y1"] for part in parts),
            heights[len(heights) // 2],
        )
    size_type, size_confidence = parse_size_type(rows, anchor)
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
