"""Financial-databook "basis/stage" column detection.

Real FDD databooks routinely lay out the same set of reporting dates 2-4
times side by side under different bases (Mgt acc / Audited / Indicative
adjusted / ...) with only a text label distinguishing the groups -- no cell
merging or other structural hint. Asking the model to eyeball which of
10-30+ columns is the "Indicative adjusted" group, on every databook, is
exactly the kind of mechanical sub-problem that drifts under prompt-only
instructions -- get it wrong and the wrong basis's figures get cited
throughout an FDD report, silently.

CANONICAL_STAGE_LABELS is ported from a sibling project's fdd_utils/workbook.py
(canonical_stage_label), which encodes label variants (Traditional/Simplified
Chinese, a client-specific phrasing, common typos) discovered by actually
hitting them in production databooks -- not something inferable from a single
example. The scanning strategy below (find the row with the densest
concentration of recognized labels, rather than the reference project's
fixed-row-offset-from-first-match approach) is new, simpler code written for
this port; it does not replicate the reference's BS/IS-section-splitting or
remark-column-filtering machinery, which yucode's simpler "which columns are
this stage" question doesn't need.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

# Ordered so a longer/more-specific variant is matched before a shorter one
# that could otherwise false-positive as a substring. "示意性调整数" ("indicative
# adjustment FIGURE") is a client-specific way of writing "示意性调整后" (after
# indicative adjustment) -- the "数" suffix here mirrors "审定数" (audited
# FIGURE) rather than meaning "the adjustment amount itself" -- so it must be
# checked before "Indicative adjustment" below, or it would fall through to
# matching the shorter "示意性调整" substring as the delta stage instead of the
# final-balance stage this is actually naming.
CANONICAL_STAGE_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Indicative adjusted", ("indicative adjusted", "indivative adjusted", "示意性调整后", "示意性調整後",
                              "示意性调整数", "示意性調整數")),
    ("Indicative adjustment", ("indicative adjustment", "indivative adjustment", "示意性调整", "示意性調整")),
    ("Audited", ("audited", "审定数", "審定數")),
    ("Audit adjustment", ("audit adjustment", "审计调整", "審計調整")),
    ("Mgt acc", ("mgt acc", "management account", "管理层数", "管理層數")),
)

UNIT_MARKERS: tuple[str, ...] = (
    "CNY'000", "USD'000", "HKD'000", "人民币千元", "人民幣千元", "million", "百万", "百萬",
)

_MAX_LABEL_CELL_LEN = 35
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def canonical_stage_label(value: Any) -> str | None:
    """Return the canonical stage name for a cell value, or None if it
    doesn't look like a basis/stage label at all."""
    if value is None:
        return None
    text = _normalize_spaces(str(value)).lower()
    if not text or len(text) > _MAX_LABEL_CELL_LEN:
        return None
    for canonical, variants in CANONICAL_STAGE_LABELS:
        if any(variant in text for variant in variants):
            return canonical
    return None


def contains_unit_marker(value: Any) -> str | None:
    """Return the first matching unit marker literal found in a cell value, or None."""
    if value is None:
        return None
    text = str(value)
    for marker in UNIT_MARKERS:
        if marker.lower() in text.lower():
            return marker
    return None


# Excel serial-date range covering ~1990-01-01 to ~2050-01-01. Some cells in
# real databooks hold a genuine date whose number format didn't survive
# (openpyxl then returns the raw serial float/int instead of a datetime) --
# observed directly in a real sample databook's date row, alongside
# correctly-typed datetime cells in the very same row. Bounded narrowly so a
# stray financial figure in an unrelated column doesn't get misread as a date.
_EXCEL_SERIAL_DATE_RANGE = (32874, 54970)


def _parse_date_like(value: Any) -> str | None:
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, str):
        s = value.strip()
        if _DATE_LIKE_RE.match(s):
            return s
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        low, high = _EXCEL_SERIAL_DATE_RANGE
        if low <= value <= high and float(value).is_integer():
            from openpyxl.utils.datetime import from_excel
            return from_excel(value).date().isoformat()
    return None


def locate_stage_columns(rows: list[list[Any]], *, date_lookback_rows: int = 3) -> dict[str, Any]:
    """Scan a grid of cell values (row-major, as read from a worksheet) for
    the "basis row" -- the row with the densest concentration of recognized
    stage labels -- and a nearby date row above it.

    Returns `{"found": False}` if no row has at least 2 recognized stage-label
    cells (below that, it's not plausibly a multi-basis header row). On
    success, returns per-canonical-stage column entries (0-based index +
    associated date, when one was found) plus the detected unit marker.
    """
    best_row_idx = -1
    best_matches: dict[int, str] = {}
    for row_idx, row in enumerate(rows):
        matches: dict[int, str] = {}
        for col_idx, cell in enumerate(row):
            canonical = canonical_stage_label(cell)
            if canonical:
                matches[col_idx] = canonical
        if len(matches) > len(best_matches):
            best_row_idx = row_idx
            best_matches = matches

    if best_row_idx < 0 or len(best_matches) < 2:
        return {"found": False}

    date_row_idx: int | None = None
    dates_by_col: dict[int, str] = {}
    for candidate_idx in range(max(0, best_row_idx - date_lookback_rows), best_row_idx):
        row = rows[candidate_idx]
        found_dates = {
            col_idx: parsed
            for col_idx in best_matches
            if col_idx < len(row) and (parsed := _parse_date_like(row[col_idx])) is not None
        }
        if len(found_dates) > len(dates_by_col):
            date_row_idx = candidate_idx
            dates_by_col = found_dates

    unit_marker: str | None = None
    for row in rows[: best_row_idx + 1]:
        for cell in row:
            unit_marker = contains_unit_marker(cell)
            if unit_marker:
                break
        if unit_marker:
            break

    stage_columns: dict[str, list[dict[str, Any]]] = {}
    for col_idx, canonical in sorted(best_matches.items()):
        entry: dict[str, Any] = {"col_idx": col_idx}
        if col_idx in dates_by_col:
            entry["date"] = dates_by_col[col_idx]
        stage_columns.setdefault(canonical, []).append(entry)

    return {
        "found": True,
        "basis_row_idx": best_row_idx,
        "date_row_idx": date_row_idx,
        "unit_marker": unit_marker,
        "stage_columns": stage_columns,
    }
