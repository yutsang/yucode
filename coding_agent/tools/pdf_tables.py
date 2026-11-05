"""Coordinate-based PDF table extraction for financial statements.

Implements the bilingual "column-safe" extraction protocol from the
financial-statement-snip skill's extraction guide as a deterministic tool,
instead of asking the model to re-derive it with ad-hoc scripts each run:

- period (year) headers are detected by their x-centres;
- each value column becomes a horizontal band around its header's x-centre,
  split at the midpoints between adjacent headers;
- words are grouped into visual rows by y-coordinate;
- a row's values are assigned to periods by which band their x-centre falls
  in — NEVER by token/reading order (bilingual PDFs interleave left-language
  labels, values, and right-language translated labels unpredictably);
- a printed dash counts as 0 only when the dash itself sits inside a band;
- label-only lines adjacent to a valued row are stitched into its label
  (wrapped row descriptions), not treated as separate numeric rows.

Pure Python on pdfplumber — no poppler/pdftotext or any other external
executable, which locked-down corporate machines cannot run.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBER_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
_DASH_TOKENS = {"-", "–", "—"}
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Words on the same visual row rarely drift more than a couple of points.
_ROW_Y_TOLERANCE = 2.5
# A label-only line further than this from any valued row is standalone
# (section headings etc.), not a wrapped continuation.
_STITCH_MAX_GAP = 14.0


def _parse_number(token: str) -> float | None:
    text = token.strip()
    if text in _DASH_TOKENS:
        return 0.0
    if not _NUMBER_RE.match(text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value == int(value):
        value = int(value)
    return -value if negative else value


def extract_page_words(pdf_path: str, page_number: int) -> list[dict[str, Any]]:
    """All words on a 1-based page with their coordinates and x-centres."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            raise ValueError(f"page {page_number} out of range (PDF has {len(pdf.pages)} pages)")
        page = pdf.pages[page_number - 1]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    return [
        {
            "text": w["text"],
            "x0": round(float(w["x0"]), 2),
            "x1": round(float(w["x1"]), 2),
            "x_center": round((float(w["x0"]) + float(w["x1"])) / 2, 2),
            "top": round(float(w["top"]), 2),
            "bottom": round(float(w["bottom"]), 2),
        }
        for w in words
    ]


def _group_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= _ROW_Y_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    return rows


def _detect_period_header(
    rows: list[list[dict[str, Any]]],
    periods: list[str] | None,
) -> tuple[list[str], list[float], float] | None:
    """Find the topmost row containing ALL requested periods (or, with no
    explicit periods, 2+ distinct year-like tokens). Returns (labels,
    x_centres left->right, header_bottom_y)."""
    for row in rows:
        if periods:
            hits: dict[str, dict[str, Any]] = {}
            for want in periods:
                for word in row:
                    if word["text"] == want and want not in hits:
                        hits[want] = word
            if len(hits) == len(periods):
                ordered = sorted(hits.values(), key=lambda w: w["x_center"])
                labels = [w["text"] for w in ordered]
                return labels, [w["x_center"] for w in ordered], max(w["bottom"] for w in ordered)
        else:
            years = [w for w in row if _YEAR_RE.match(w["text"])]
            distinct = {w["text"] for w in years}
            if len(distinct) >= 2:
                first: dict[str, dict[str, Any]] = {}
                for word in years:
                    first.setdefault(word["text"], word)
                ordered = sorted(first.values(), key=lambda w: w["x_center"])
                return (
                    [w["text"] for w in ordered],
                    [w["x_center"] for w in ordered],
                    max(w["bottom"] for w in ordered),
                )
    return None


def _build_bands(centres: list[float]) -> list[tuple[float, float]]:
    """One (left, right) band per period column. Adjacent bands split at the
    midpoint between header x-centres; outer edges extend by half the
    typical inter-column gap so right-aligned digits stay inside."""
    if len(centres) == 1:
        return [(centres[0] - 60, centres[0] + 60)]
    gaps = [centres[i + 1] - centres[i] for i in range(len(centres) - 1)]
    margin = max(min(gaps) / 2, 20.0)
    bands: list[tuple[float, float]] = []
    for i, centre in enumerate(centres):
        left = centres[i - 1] + gaps[i - 1] / 2 if i > 0 else centre - margin
        right = centres[i] + gaps[i] / 2 if i < len(centres) - 1 else centre + margin
        bands.append((left, right))
    return bands


def extract_period_table(
    pdf_path: str,
    page_number: int,
    periods: list[str] | None = None,
) -> dict[str, Any]:
    """Extract a period-columned table from one page using x-coordinate
    banding. Returns {found, periods, rows, skipped_label_rows} where each
    row is {label, values (one slot per period; None = nothing printed in
    that band), right_label, top}."""
    words = extract_page_words(pdf_path, page_number)
    if not words:
        return {"found": False, "reason": "page has no text layer — flag for visual review, do not guess"}

    rows = _group_rows(words)
    header = _detect_period_header(rows, periods)
    if header is None:
        wanted = f" {periods}" if periods else ""
        return {"found": False, "reason": f"no period header row{wanted} detected on page {page_number}"}
    labels, centres, header_bottom = header
    bands = _build_bands(centres)
    label_right_edge = bands[0][0]
    values_right_edge = bands[-1][1]

    extracted: list[dict[str, Any]] = []
    for row in rows:
        if all(w["bottom"] <= header_bottom for w in row):
            continue  # header row itself or anything above it
        values: list[float | None] = [None] * len(bands)
        label_parts: list[str] = []
        right_parts: list[str] = []
        for word in sorted(row, key=lambda w: w["x0"]):
            centre = word["x_center"]
            band_index = next(
                (i for i, (left, right) in enumerate(bands) if left <= centre <= right), None,
            )
            if band_index is not None:
                parsed = _parse_number(word["text"])
                # Same band twice on one line (e.g. a note ref drifting in)
                # keeps the FIRST parsed value; ambiguity stays visible
                # because the second token lands in the label/right_label.
                if parsed is not None and values[band_index] is None:
                    values[band_index] = parsed
                    continue
            if centre < label_right_edge:
                label_parts.append(word["text"])
            elif centre > values_right_edge:
                right_parts.append(word["text"])
            else:
                # Non-numeric word inside the value area — keep it visible
                # in the label rather than dropping it silently.
                label_parts.append(word["text"])
        extracted.append({
            "label": " ".join(label_parts),
            "values": values,
            "right_label": " ".join(right_parts),
            "top": row[0]["top"],
        })

    # Stitch wrapped labels. A label-only line can be the tail of the
    # PREVIOUS valued row's label or the head of the NEXT one's — genuinely
    # ambiguous from coordinates alone, so when both neighbours are within
    # tolerance the CLOSER one (by y-distance) wins, ties preferring the
    # next row (the guide's canonical example wraps the label above its
    # values). Consecutive label-only lines merge into one blob first.
    # Anything not adjacent to a valued row is a standalone heading —
    # reported in skipped_label_rows, never invented into a numeric row.
    blobs: list[dict[str, Any]] = []  # valued rows + merged label-only blobs
    for row in extracted:
        has_values = any(v is not None for v in row["values"])
        if not has_values and not row["label"]:
            continue
        if not has_values and blobs and blobs[-1]["kind"] == "label" \
                and row["top"] - blobs[-1]["last_top"] <= _STITCH_MAX_GAP:
            blob = blobs[-1]
            blob["label"] = f"{blob['label']} {row['label']}".strip()
            blob["last_top"] = row["top"]
            continue
        blobs.append({
            "kind": "row" if has_values else "label",
            "label": row["label"],
            "values": row["values"],
            "right_label": row["right_label"],
            "top": row["top"],
            "last_top": row["top"],
        })

    final_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for i, blob in enumerate(blobs):
        if blob["kind"] == "row":
            final_rows.append({
                "label": blob["label"], "values": blob["values"],
                "right_label": blob["right_label"], "top": round(blob["top"], 2),
            })
            continue
        prev_blob = blobs[i - 1] if i > 0 and blobs[i - 1]["kind"] == "row" else None
        next_blob = blobs[i + 1] if i + 1 < len(blobs) and blobs[i + 1]["kind"] == "row" else None
        dist_prev = blob["top"] - prev_blob["last_top"] if prev_blob is not None else None
        dist_next = next_blob["top"] - blob["last_top"] if next_blob is not None else None
        attach_prev = dist_prev is not None and dist_prev <= _STITCH_MAX_GAP
        attach_next = dist_next is not None and dist_next <= _STITCH_MAX_GAP
        if attach_prev and attach_next:
            attach_prev = dist_prev < dist_next  # tie -> next
        if attach_prev and final_rows:
            final_rows[-1]["label"] = f"{final_rows[-1]['label']} {blob['label']}".strip()
            if blob["right_label"]:
                final_rows[-1]["right_label"] = (
                    f"{final_rows[-1]['right_label']} {blob['right_label']}".strip()
                )
        elif attach_next:
            next_blob["label"] = f"{blob['label']} {next_blob['label']}".strip()
            if blob["right_label"]:
                next_blob["right_label"] = (
                    f"{blob['right_label']} {next_blob['right_label']}".strip()
                )
        else:
            skipped.append(blob["label"])

    return {
        "found": True,
        "periods": labels,
        "band_x_ranges": [[round(left, 1), round(right, 1)] for left, right in bands],
        "rows": final_rows,
        "skipped_label_rows": skipped,
    }
