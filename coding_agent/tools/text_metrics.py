"""Pillow-based text fitting for PPTX shapes.

Predicts how text will wrap inside a PPTX text frame using the actual font's
glyph advance widths, instead of a chars-per-inch constant that treats every
glyph as the same width (wrong for any real font, and worse for mixed CJK +
Latin text). Ported from a sibling project's fdd_utils/text_metrics.py, which
verified this approach against real PowerPoint renders on a client box.

The calibrated constants in ESTIMATE_FONT_SIZE_PT / ESTIMATE_LINE_SPACING /
ESTIMATE_PARA_GAP_PT come from that same project's production debugging
(see estimate_capacity's docstring) -- they are not arbitrary defaults.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import ImageFont

EMU_PER_INCH = 914400
EMU_PER_POINT = 12700

# OOXML bodyPr default insets (ECMA-376 spec), used when the attribute is absent.
_DEFAULT_LINS_EMU = 91440   # 0.10"
_DEFAULT_RINS_EMU = 91440   # 0.10"
_DEFAULT_TINS_EMU = 45720   # 0.05"
_DEFAULT_BINS_EMU = 45720   # 0.05"

# Empirically calibrated in the reference project against a real client
# PowerPoint export -- assuming 0.9 line spacing and 6-9pt inter-paragraph
# gaps (plausible-looking defaults) made computed capacity ~30% smaller than
# the box's true capacity. These are the values actually applied to a
# textMainBullets-style commentary run, regardless of language.
ESTIMATE_FONT_SIZE_PT = 9.0
ESTIMATE_LINE_SPACING = 1.0
ESTIMATE_PARA_GAP_PT = 3.0

_LATIN_FALLBACKS = {
    "Darwin": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "Windows": [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
    ],
    "Linux": [
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}

_CJK_FALLBACKS = {
    "Darwin": [
        "/Library/Fonts/Microsoft/Microsoft YaHei.ttf",
        "/Library/Fonts/Microsoft YaHei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ],
    "Windows": [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ],
    "Linux": [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ],
}

# Characters PowerPoint/Word never let start a line (行首禁则/kinsoku) -- closing
# brackets/quotes and sentence-ending punctuation get pulled back onto the
# previous line even if that makes it slightly exceed the nominal width.
# Without this, our greedy per-character CJK wrap can predict one line more
# than PowerPoint actually renders, making capacity look more used-up than it
# really is.
_KINSOKU_NO_LINE_START = set("。,、;:?!)]}%％、。；：？！）】》」』〉…‥,.;:?!)]}’”")

_CJK_RANGES = (
    (0x3000, 0x303F), (0x3040, 0x309F), (0x30A0, 0x30FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
)


def _is_cjk_char(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch[0])
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def is_predominantly_chinese(text: str) -> bool:
    """>30% CJK characters, not "contains any CJK character" -- an English
    commentary that merely names a Chinese counterparty/person is still
    fundamentally Latin-script prose and should wrap/measure as such."""
    non_space = [ch for ch in str(text or "") if not ch.isspace()]
    if not non_space:
        return False
    cjk_count = sum(1 for ch in non_space if _is_cjk_char(ch))
    return (cjk_count / len(non_space)) > 0.3


def _apply_kinsoku(lines: list[str]) -> list[str]:
    if len(lines) < 2:
        return lines
    result = list(lines)
    i = 1
    while i < len(result):
        line = result[i]
        if line and result[i - 1] and line[0] in _KINSOKU_NO_LINE_START:
            result[i - 1] = result[i - 1] + line[0]
            result[i] = line[1:]
            if not result[i]:
                del result[i]
            continue
        i += 1
    return result


def _fc_match(family: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.check_output(
            ["fc-match", "-f", "%{file}", family],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
        return out or None
    except (subprocess.SubprocessError, OSError):
        return None


@lru_cache(maxsize=64)
def resolve_font_path(family: str, *, is_cjk: bool) -> str | None:
    if os.path.isabs(family) and os.path.exists(family):
        return family
    system = platform.system()
    candidates = list(_CJK_FALLBACKS.get(system, []) if is_cjk else _LATIN_FALLBACKS.get(system, []))
    for path in candidates:
        if os.path.exists(path):
            return path
    fc = _fc_match(family)
    if fc and os.path.exists(fc):
        return fc
    return None


_FONT_CACHE: dict[tuple[str, float, bool], Any] = {}


def get_font(family: str, size_pt: float, *, is_cjk: bool) -> ImageFont.FreeTypeFont:
    key = (family, round(size_pt, 3), is_cjk)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    path = resolve_font_path(family, is_cjk=is_cjk)
    if path is None:
        raise FileNotFoundError(
            f"Could not resolve a font file for family={family!r} is_cjk={is_cjk} "
            f"on this machine. Capacity estimation needs a real font file to measure "
            f"glyph widths -- treat this tool as unavailable and fall back to the "
            f"rough chars-per-inch heuristic instead."
        )
    font = ImageFont.truetype(path, size=size_pt)
    _FONT_CACHE[key] = font
    return font


@dataclass(frozen=True)
class TextBox:
    width_pt: float
    height_pt: float


def text_box_from_shape(shape: Any) -> TextBox:
    """Effective writable text box of a python-pptx shape, in points --
    shape size minus the text frame's actual (or OOXML-default) insets."""
    body_pr = None
    try:
        body_pr = shape.text_frame._txBody.bodyPr
    except Exception:
        pass

    def _attr(name: str, default: int) -> int:
        if body_pr is None:
            return default
        v = body_pr.get(name)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    l_ins = _attr("lIns", _DEFAULT_LINS_EMU)
    r_ins = _attr("rIns", _DEFAULT_RINS_EMU)
    t_ins = _attr("tIns", _DEFAULT_TINS_EMU)
    b_ins = _attr("bIns", _DEFAULT_BINS_EMU)

    width_emu = max(0, int(shape.width) - l_ins - r_ins)
    height_emu = max(0, int(shape.height) - t_ins - b_ins)
    return TextBox(
        width_pt=width_emu / EMU_PER_POINT,
        height_pt=height_emu / EMU_PER_POINT,
    )


def _measure(font: ImageFont.FreeTypeFont, text: str) -> float:
    return float(font.getlength(text)) if text else 0.0


def _atomize(text: str) -> list[tuple[str, str]]:
    """Split into (separator, atom) pairs -- an atom is one CJK character or
    a Latin word; separator is " " between two Latin atoms, "" otherwise."""
    atoms: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if _is_cjk_char(ch):
            atoms.append(("", ch))
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and not _is_cjk_char(text[j]):
            j += 1
        word = text[i:j]
        sep = " " if atoms and not _is_cjk_char(atoms[-1][1][-1]) else ""
        atoms.append((sep, word))
        i = j
    return atoms


def wrap_paragraph(text: str, font: ImageFont.FreeTypeFont, max_width_pt: float) -> list[str]:
    """Greedy word wrap of a single paragraph using real glyph widths."""
    if not text or not text.strip():
        return [""]
    if max_width_pt <= 0:
        return [text]

    atoms = _atomize(text)
    if not atoms:
        return [""]

    lines: list[str] = []
    current = ""
    for sep, word in atoms:
        candidate = word if not current else current + sep + word
        if _measure(font, candidate) <= max_width_pt:
            current = candidate
            continue
        if current:
            lines.append(current)
        if _measure(font, word) > max_width_pt:
            sub = ""
            for ch in word:
                if _measure(font, sub + ch) <= max_width_pt:
                    sub += ch
                else:
                    if sub:
                        lines.append(sub)
                    sub = ch
            current = sub
        else:
            current = word
    if current:
        lines.append(current)
    return _apply_kinsoku(lines) if lines else [""]


def line_height_pt(font: ImageFont.FreeTypeFont, *, line_spacing: float = 1.0) -> float:
    ascent, descent = font.getmetrics()
    return (ascent + descent) * line_spacing


def estimate_capacity(
    shape: Any,
    paragraphs: list[str],
    *,
    is_chinese: bool | None = None,
) -> dict[str, Any]:
    """Estimate whether `paragraphs` (as they would be written by
    fill_pptx_shape_text -- one list entry per paragraph) fit in `shape`,
    using the same font size / line spacing / inter-paragraph gap actually
    applied to a commentary run in the reference project (9pt, single
    spacing, 3pt space-after per paragraph -- see module docstring)."""
    joined = "\n".join(paragraphs)
    chinese = is_predominantly_chinese(joined) if is_chinese is None else is_chinese
    family = "Microsoft YaHei" if chinese else "Arial"
    font = get_font(family, ESTIMATE_FONT_SIZE_PT, is_cjk=chinese)
    box = text_box_from_shape(shape)
    line_h = line_height_pt(font, line_spacing=ESTIMATE_LINE_SPACING)

    per_paragraph = []
    used_height_pt = 0.0
    for para in paragraphs:
        lines = wrap_paragraph(para, font, box.width_pt)
        para_height = len(lines) * line_h + ESTIMATE_PARA_GAP_PT
        used_height_pt += para_height
        per_paragraph.append({"line_count": len(lines), "lines": lines})

    remaining_height_pt = box.height_pt - used_height_pt
    return {
        "fits": used_height_pt <= box.height_pt,
        "is_chinese": chinese,
        "font_size_pt": ESTIMATE_FONT_SIZE_PT,
        "box_width_pt": round(box.width_pt, 1),
        "box_height_pt": round(box.height_pt, 1),
        "used_height_pt": round(used_height_pt, 1),
        "remaining_height_pt": round(remaining_height_pt, 1),
        "fill_ratio": round(used_height_pt / box.height_pt, 2) if box.height_pt > 0 else None,
        "remaining_lines_estimate": max(0, int(remaining_height_pt // line_h)) if line_h > 0 else 0,
        "per_paragraph": per_paragraph,
    }
