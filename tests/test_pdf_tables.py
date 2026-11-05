"""Coordinate-based PDF table extraction (pdf_tables.py) — the deterministic
core of the financial-statement-snip skill's bilingual column-safe protocol.

The fixture is a hand-assembled minimal PDF (raw bytes, positioned text
objects) mimicking a bilingual cash note's layout: left-language labels on
the left, 2025/2024 value columns in the middle, translated labels on the
right. Synthetic data only. The layout mirrors the extraction guide's
regression example: values must map by x-coordinate to year bands — a swap
([25641, 1410] instead of [1410, 25641]) is the exact failure this exists
to prevent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")

from coding_agent.tools import pdf_tables  # noqa: E402


def _make_pdf(texts: list[tuple[float, float, str]]) -> bytes:
    """Assemble a one-page PDF placing each (x, y, text) with Helvetica 8.
    y is measured from the BOTTOM (PDF convention)."""
    content_lines = ["BT", "/F1 8 Tf"]
    for x, y, text in texts:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content_lines.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


# Header x-centres ~340 (2025) and ~440 (2024); numbers right-aligned so
# their text starts left of the header centre. Right-language labels at
# x>=505, outside the last band's right edge (440 + 50 = 490).
_BILINGUAL_NOTE = [
    (60, 720, "4."), (75, 720, "Kas dan setara kas - neto"),
    (330, 700, "2025"), (430, 700, "2024"),
    (60, 680, "Kas"), (322, 680, "1,410"), (415, 680, "25,641"), (505, 680, "Cash"),
    (60, 665, "PT Bank HSBC Indonesia - Rupiah"),
    (295, 665, "17,948,575"), (398, 665, "4,697,031"), (505, 665, "Bank Rupiah"),
    # Wrapped label: first line has no values; values sit on the SECOND line.
    (60, 650, "PT Bank Maybank Indonesia,"),
    (60, 638, "Tbk. - Rupiah deposit"),
    (290, 638, "92,000,000"), (390, 638, "100,000,000"), (505, 638, "Deposito Rupiah"),
    # Dash under 2025 only; real value under 2024.
    (60, 620, "PT Bank OCBC NISP, Tbk."), (338, 620, "-"), (390, 620, "47,000,000"),
    (505, 620, "Deposito"),
    # Bracketed negatives.
    (60, 600, "Allowance for impairment losses"),
    (318, 600, "(4,983)"), (418, 600, "(8,741)"), (505, 600, "Penyisihan"),
    (60, 580, "Cash and cash equivalents - net"),
    (280, 580, "232,335,365"), (380, 580, "255,845,815"), (505, 580, "Neto"),
]


@pytest.fixture()
def bilingual_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "note.pdf"
    path.write_bytes(_make_pdf(_BILINGUAL_NOTE))
    return path


class TestExtractPageWords:
    def test_words_carry_coordinates(self, bilingual_pdf: Path) -> None:
        words = pdf_tables.extract_page_words(str(bilingual_pdf), 1)
        assert words, "no words extracted from the synthetic PDF"
        kas = next(w for w in words if w["text"] == "Kas")
        assert kas["x0"] < 100
        assert {"text", "x0", "x1", "x_center", "top", "bottom"} <= set(kas.keys())

    def test_page_out_of_range(self, bilingual_pdf: Path) -> None:
        with pytest.raises(ValueError, match="out of range"):
            pdf_tables.extract_page_words(str(bilingual_pdf), 7)


class TestExtractPeriodTable:
    def test_detects_year_headers(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        assert result["found"] is True
        assert result["periods"] == ["2025", "2024"]

    def test_values_map_by_x_band_never_by_token_order(self, bilingual_pdf: Path) -> None:
        """The regression case from the extraction guide: Kas must be
        [1410, 25641], never [25641, 1410]."""
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        kas = next(r for r in result["rows"] if r["label"].startswith("Kas"))
        assert kas["values"] == [1410, 25641]

    def test_right_language_labels_never_become_values(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        kas = next(r for r in result["rows"] if r["label"].startswith("Kas"))
        assert kas["right_label"] == "Cash"
        hsbc = next(r for r in result["rows"] if "HSBC" in r["label"])
        assert hsbc["values"] == [17948575, 4697031]
        assert "Bank Rupiah" in hsbc["right_label"]

    def test_dash_is_zero_only_in_its_band(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        ocbc = next(r for r in result["rows"] if "OCBC" in r["label"])
        assert ocbc["values"] == [0, 47000000]

    def test_bracketed_figures_are_negative(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        allowance = next(r for r in result["rows"] if "Allowance" in r["label"])
        assert allowance["values"] == [-4983, -8741]

    def test_wrapped_label_stitched_onto_valued_row(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        maybank = next(r for r in result["rows"] if "Maybank" in r["label"])
        assert "PT Bank Maybank Indonesia," in maybank["label"]
        assert "Tbk. - Rupiah deposit" in maybank["label"]
        assert maybank["values"] == [92000000, 100000000]

    def test_explicit_periods_filter(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1, ["2025", "2024"])
        assert result["found"] is True
        assert result["periods"] == ["2025", "2024"]

    def test_missing_periods_returns_found_false(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1, ["2030", "2029"])
        assert result["found"] is False
        assert "no period header" in result["reason"]

    def test_bottom_line_total_row(self, bilingual_pdf: Path) -> None:
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        net = next(r for r in result["rows"] if "net" in r["label"])
        assert net["values"] == [232335365, 255845815]

    def test_note_title_reported_as_skipped_not_numeric(self, bilingual_pdf: Path) -> None:
        """The note title line above the header must not become a numeric
        row (it's above the header band and excluded entirely)."""
        result = pdf_tables.extract_period_table(str(bilingual_pdf), 1)
        assert not any("Kas dan setara kas" in r["label"] for r in result["rows"])


class TestToolLevel:
    def test_extract_pdf_period_table_tool(self, tmp_path: Path) -> None:
        import json

        from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
        from coding_agent.tools import ToolRegistry
        (tmp_path / "note.pdf").write_bytes(_make_pdf(_BILINGUAL_NOTE))
        config = AppConfig(
            provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        registry = ToolRegistry(tmp_path, config)
        out = json.loads(registry.execute("extract_pdf_period_table", {"path": "note.pdf", "page": 1}))
        assert out["found"] is True
        kas = next(r for r in out["rows"] if r["label"].startswith("Kas"))
        assert kas["values"] == [1410, 25641]

        words_out = json.loads(registry.execute("extract_pdf_words", {"path": "note.pdf", "page": 1}))
        assert words_out["count"] > 10
