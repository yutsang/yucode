"""Font vendoring and measurement for the office golden suite.

Two things live here, and the second is the reason the first exists.

**Vendoring.** `estimate_pptx_text_capacity` and `fill_pptx_shape_text` are
functions of the actual glyph metrics of whatever font resolved on the machine
that ran them. On this Mac that is `/System/Library/Fonts/Supplemental/Arial.ttf`
and `/System/Library/Fonts/STHeiti Light.ttc`; on a Linux CI runner it is
neither. A golden keyed to a system font is a golden that only one machine can
check. So the suite ships two OFL subsets and patches the resolver to use them,
and runs the same cases a second time against whatever the system has — the
portable pass is the contract, the system pass is diagnostic.

**Measurement, and why the port cannot skip it.** Pillow here has no raqm
(verified `PIL.features.check("raqm") is False`), so it runs `Layout.BASIC` and
`getlength` loads each glyph with `FT_LOAD_DEFAULT` — which means every advance
is grid-fit to a whole pixel by the TrueType hinting interpreter. Measured on
Arial at 9 pt, 21 of the 95 printable ASCII glyphs have a hinted advance that
`round(unhinted)` gets wrong:

    W  hinted 9.0  unhinted 8.370      m  hinted 8.0  unhinted 7.497
    C  hinted 7.0  unhinted 6.500      F  hinted 6.0  unhinted 5.498
    (the full set: C D F H J M N R T U W Z c k m s v w x y z)

A TypeScript port reading `hmtx` through fontkit or opentype.js cannot
reproduce those by arithmetic — no rounding rule fixes all 21 without breaking
the other 74. `advances_px` in the manifest is therefore not a convenience: it
is the only transferable form the measurement has, and `fonts.ts` has to ship
it as a table.

Two things the measurement says that are easy to assume the other way round.

The divergence belongs to the font, not to hinting as such. The vendored Noto
Sans subset has **zero** mismatches across the same 95 glyphs, so the
`portable` pass alone would not catch a port that computed advances by
rounding. The `system` pass would — and Arial is what the target box actually
resolves, so the operationally relevant numbers are Arial's. Both variants
record `unhinted_px` and `round_unhinted_mismatches` per glyph so this stays
visible rather than having to be rediscovered.

CJK is clean either way: STHeiti Light and the vendored Noto Sans SC subset
both matched on all 37 glyphs tested, because CJK advances are uniform-width
and land on integers to begin with.

The subsets keep their hinting regardless. Stripping it would guarantee the
portable oracle was structurally unlike the system one, which is the opposite
of what a portable fallback is for.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = REPO_ROOT / "tests" / "golden" / "fonts"

LATIN_FILE = "YucodeGoldenLatin-Regular.ttf"
CJK_FILE = "YucodeGoldenCJK-Regular.ttf"
SOURCES_FILE = "SOURCES.json"

FONT_VARIANTS = ("portable", "system")

# text_metrics.py:40-42 — the calibrated constants every capacity case uses.
ESTIMATE_FONT_SIZE_PT = 9.0


@dataclass(frozen=True)
class FontBinding:
    variant: str
    latin_path: Path | None
    cjk_path: Path | None

    @property
    def available(self) -> bool:
        return self.latin_path is not None and self.cjk_path is not None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# resolving a binding
# --------------------------------------------------------------------------


def resolve_font_binding(variant: str) -> FontBinding:
    if variant == "portable":
        latin, cjk = FONTS_DIR / LATIN_FILE, FONTS_DIR / CJK_FILE
        if not (latin.is_file() and cjk.is_file()):
            raise SystemExit(
                f"vendored fonts missing under {FONTS_DIR}. Run:\n"
                "  python tools/golden/capture_office_goldens.py vendor-fonts "
                "--latin <NotoSans-Regular.ttf> --cjk <NotoSansSC[wght].ttf>"
            )
        _verify_against_sources(latin, cjk)
        return FontBinding("portable", latin, cjk)

    if variant == "system":
        from coding_agent.tools import text_metrics

        # Clear both caches first: a portable pass may have run before this
        # one, and neither cache key includes the resolved path.
        text_metrics.resolve_font_path.cache_clear()
        text_metrics._FONT_CACHE.clear()
        latin_str = text_metrics.resolve_font_path("Arial", is_cjk=False)
        cjk_str = text_metrics.resolve_font_path("Microsoft YaHei", is_cjk=True)
        return FontBinding(
            "system",
            Path(latin_str) if latin_str else None,
            Path(cjk_str) if cjk_str else None,
        )

    raise ValueError(f"unknown font variant: {variant}")


def _verify_against_sources(latin: Path, cjk: Path) -> None:
    sources_path = FONTS_DIR / SOURCES_FILE
    if not sources_path.is_file():
        raise SystemExit(f"{sources_path} missing; the subsets are unauditable without it")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    for path, key in ((latin, "latin"), (cjk, "cjk")):
        expected = sources[key]["output_sha256"]
        actual = sha256_file(path)
        if actual != expected:
            raise SystemExit(
                f"vendored font {path.name} does not match SOURCES.json "
                f"({actual[:12]} vs {expected[:12]}); re-run vendor-fonts"
            )


@contextlib.contextmanager
def font_binding(binding: FontBinding) -> Iterator[None]:
    """Point `text_metrics.resolve_font_path` at this binding for the block.

    `estimate_capacity` hardcodes the families "Arial" and "Microsoft YaHei"
    (text_metrics.py:303), so the absolute-path escape hatch at
    text_metrics.py:146 is unreachable from tool arguments — patching the
    module attribute is the only seam.

    Both caches are cleared on the way in AND on the way out. `_FONT_CACHE`'s
    key is `(family, round(size, 3), is_cjk)` (text_metrics.py:164), which does
    not include the resolved path, so a stale entry from the other variant
    would be reused silently across the boundary. `resolve_font_path` is
    `lru_cache`d on `(family, is_cjk)` for the same reason; keeping the
    original in a local is what makes `cache_clear()` reachable after the
    attribute has been swapped.
    """
    from coding_agent.tools import text_metrics

    original = text_metrics.resolve_font_path

    def _patched(family: str, *, is_cjk: bool) -> str | None:
        chosen = binding.cjk_path if is_cjk else binding.latin_path
        return str(chosen) if chosen else None

    original.cache_clear()
    text_metrics._FONT_CACHE.clear()
    if binding.variant != "system":
        text_metrics.resolve_font_path = _patched
    try:
        yield
    finally:
        text_metrics.resolve_font_path = original
        original.cache_clear()
        text_metrics._FONT_CACHE.clear()


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


def font_fingerprint(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    from fontTools.ttLib import TTFont, TTCollection

    if path.suffix.lower() == ".ttc":
        # Pillow opens a collection at index 0; mirror that.
        font = TTCollection(str(path), lazy=True).fonts[0]
    else:
        font = TTFont(str(path), lazy=True, fontNumber=0)
    # A vendored font is named repo-relative so the manifest is the same on
    # every machine; a system font keeps its absolute path, because which file
    # resolved IS the fact being recorded.
    try:
        display = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        display = str(path)
    return {
        "path": display,
        "sha256": sha256_file(path),
        "postscript_name": font["name"].getDebugName(6),
        "family": font["name"].getDebugName(1),
        "units_per_em": font["head"].unitsPerEm,
        "outlines": "glyf" if "glyf" in font else ("cff" if "CFF " in font else "unknown"),
    }


def measure(path: Path, chars: str, size_pt: float) -> dict[str, object]:
    """Record what Pillow actually reports for this font at this size.

    `advances_px` is per character via `getlength`, which is what
    `text_metrics._atomize`/`wrap` consume one atom at a time.
    `unhinted_px` is the same advance computed from `hmtx` alone, included so
    the TypeScript side can see, per glyph, whether arithmetic would have got
    it right — 13 ASCII glyphs on Arial 9pt where it does not.
    """
    from fontTools.ttLib import TTFont, TTCollection
    from PIL import ImageFont

    pil = ImageFont.truetype(str(path), size_pt, index=0)
    ascent, descent = pil.getmetrics()

    if path.suffix.lower() == ".ttc":
        tt = TTCollection(str(path), lazy=True).fonts[0]
    else:
        tt = TTFont(str(path), lazy=True, fontNumber=0)
    upem = tt["head"].unitsPerEm
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]
    glyph_order = tt.getGlyphOrder()
    notdef = glyph_order[0] if glyph_order else None

    advances: dict[str, float] = {}
    unhinted: dict[str, float] = {}
    for char in sorted(set(chars)):
        advances[char] = pil.getlength(char)
        name = cmap.get(ord(char), notdef)
        if name is not None and name in hmtx.metrics:
            unhinted[char] = hmtx.metrics[name][0] * size_pt / upem
    mismatches = sorted(
        char for char, value in unhinted.items() if round(value) != advances[char]
    )
    return {
        "size_pt": size_pt,
        "ascent_px": ascent,
        "descent_px": descent,
        "line_height_px": ascent + descent,
        "advances_px": advances,
        "unhinted_px": {k: round(v, 4) for k, v in unhinted.items()},
        "round_unhinted_mismatches": mismatches,
    }


def font_block(binding: FontBinding, chars: str) -> dict[str, object]:
    block: dict[str, object] = {"variant": binding.variant, "available": binding.available}
    # _BASE_CHARS, not just the characters the cases happen to use. The
    # subsets are built over both (see vendor_fonts), so measuring only the
    # case text shipped a 58-glyph table for a font that renders 95 — and the
    # first port test that types a W had no oracle. Four of the glyphs whose
    # hinted advance differs from round(hmtx) on Arial were among the missing.
    chars = _BASE_CHARS + chars
    for key, path in (("latin", binding.latin_path), ("cjk", binding.cjk_path)):
        fingerprint = font_fingerprint(path)
        if fingerprint is None:
            block[key] = None
            continue
        subset = _charset_for(key, chars)
        fingerprint["metrics"] = measure(path, subset, ESTIMATE_FONT_SIZE_PT)
        block[key] = fingerprint
    return block


def _charset_for(key: str, chars: str) -> str:
    """Split by the same rule the runtime uses, not by a literal boundary.

    An earlier version split at U+2E80, which disagrees with
    `text_metrics._is_cjk_char` over U+2E81-U+2FFF (the radical blocks): a
    character there would have been measured only under the CJK font while
    the tool measured it with the Latin one. Reusing the runtime classifier
    means the two can never drift apart.
    """
    from coding_agent.tools.text_metrics import _is_cjk_char

    cjk = "".join(c for c in chars if _is_cjk_char(c))
    latin = "".join(c for c in chars if not _is_cjk_char(c))
    return cjk if key == "cjk" else latin


# --------------------------------------------------------------------------
# vendoring
# --------------------------------------------------------------------------

# The unicodes the subsets must cover. Derived rather than guessed: the caller
# passes every character any case measures, and these are added so a subset is
# never one glyph short of a future case that only adds prose.
_BASE_CHARS = "".join(chr(c) for c in range(0x20, 0x7F))


def vendor_fonts(latin_src: Path, cjk_src: Path, chars: str) -> None:
    """Subset, rename and commit the two OFL fonts.

    Renaming matters more for the CJK font than the Latin one. Noto Sans
    carries OFL 1.1 with no Reserved Font Name, so §5 does not force a rename
    there. Noto Sans SC descends from Source Han Sans and its OFL.txt reserves
    the name "Source", which our names do not contain either way. Both are
    renamed regardless: a hinted, glyph-stripped subset is not Noto Sans and
    should not claim to be, and renaming guarantees a developer's
    `fc-match "Noto Sans"` can never resolve to ours by accident. What the
    licence does require — the text travelling with the modified version and
    the copyright notice preserved — is why both OFL files and SOURCES.json sit
    beside the subsets.
    """
    from fontTools import subset as ft_subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    wanted = sorted({ord(c) for c in (_BASE_CHARS + chars)})

    sources: dict[str, object] = {
        "licence_files": ["OFL-NotoSans.txt", "OFL-NotoSansSC.txt"],
        "note": (
            "Subsets of Google's Noto fonts, SIL OFL 1.1. Renamed so they "
            "cannot be confused with the upstream families. Hinting is kept "
            "deliberately: the goldens measure hinted, grid-fit advances, and "
            "an unhinted subset would make them derivable by arithmetic and "
            "so stop testing the thing they exist to test."
        ),
        "fonttools_version": _fonttools_version(),
    }

    for key, src, out_name, family, licence_file, upstream in (
        ("latin", latin_src, LATIN_FILE, "Yucode Golden Latin",
         "OFL-NotoSans.txt", "Noto Sans (notofonts/latin-greek-cyrillic)"),
        ("cjk", cjk_src, CJK_FILE, "Yucode Golden CJK",
         "OFL-NotoSansSC.txt", "Noto Sans SC (google/fonts ofl/notosanssc)"),
    ):
        source_sha = sha256_file(src)
        # recalcTimestamp=False is load-bearing. fontTools re-stamps
        # head.modified with the current time during save() by default, AFTER
        # _freeze_head_dates has zeroed it, so the subsets carried a wall
        # clock and two builds seconds apart produced different bytes. That
        # made SOURCES.json's output_sha256 a one-shot value: anyone
        # re-running vendor-fonts to audit the subsets got a different hash,
        # and _verify_against_sources then refused every capture.
        font = TTFont(str(src), fontNumber=0, recalcTimestamp=False)
        instanced = False
        if "fvar" in font:
            # Upstream ships Noto Sans SC only as a variable font; pin Regular
            # before subsetting so the goldens describe one concrete instance.
            font = instancer.instantiateVariableFont(font, {"wght": 400})
            font.recalcTimestamp = False
            instanced = True

        options = ft_subset.Options()
        options.layout_features = []          # no GSUB/GPOS: Pillow does no
                                              # shaping here (raqm absent), and
                                              # a port that shaped would differ
        options.hinting = True
        options.desubroutinize = True
        options.notdef_outline = True
        options.recalc_bounds = True
        options.recalc_average_width = True
        options.drop_tables += ["DSIG"]
        options.name_IDs = ["*"]
        options.name_legacy = True
        options.name_languages = ["*"]

        subsetter = ft_subset.Subsetter(options=options)
        subsetter.populate(unicodes=wanted)
        subsetter.subset(font)

        _rename(font, family, out_name)
        _freeze_head_dates(font)

        target = FONTS_DIR / out_name
        font.save(str(target))
        font.close()

        sources[key] = {
            "upstream": upstream,
            "upstream_file": src.name,
            "upstream_sha256": source_sha,
            "licence": f"SIL Open Font License 1.1 (see {licence_file})",
            "instanced_wght": 400 if instanced else None,
            "subset_unicode_count": len(wanted),
            "subset_options": {
                "layout_features": [],
                "hinting": True,
                "desubroutinize": True,
                "notdef_outline": True,
                "drop_tables": ["DSIG"],
            },
            "renamed_to": family,
            "output_sha256": sha256_file(target),
            "output_bytes": target.stat().st_size,
        }

    (FONTS_DIR / SOURCES_FILE).write_text(
        json.dumps(sources, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _rename(font, family: str, filename: str) -> None:
    style = "Regular"
    full = f"{family} {style}"
    postscript = filename.rsplit(".", 1)[0]
    name_table = font["name"]
    # Replace rather than append: leaving the upstream names in place would let
    # a font manager show this subset as Noto Sans.
    name_table.names = [n for n in name_table.names if n.nameID not in (1, 2, 3, 4, 6, 16, 17)]
    for name_id, value in (
        (1, family),
        (2, style),
        (3, f"{full}; subset for yucode golden tests"),
        (4, full),
        (6, postscript),
    ):
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)


def _freeze_head_dates(font) -> None:
    # fontTools stamps head.modified with the current time, which would make
    # every --check run report drift.
    head = font["head"]
    head.created = 0
    head.modified = 0


def _fonttools_version() -> str:
    try:
        from fontTools import version

        return version
    except Exception:  # noqa: BLE001 - reporting only
        import importlib.metadata

        return importlib.metadata.version("fonttools")


def system_note() -> dict[str, object]:
    from PIL import Image, features

    return {
        "platform": platform.platform(),
        "pillow": Image.__version__,
        "raqm": features.check("raqm"),
        "freetype": features.version("freetype2"),
    }
