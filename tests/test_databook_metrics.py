"""databook_metrics: financial-databook "basis/stage" column detection, ported
from a sibling project's fdd_utils/workbook.py (canonical_stage_label). Exists
because the fdd-commentary skill's "use only the Indicative adjusted columns"
instruction asked the model to visually disambiguate 10-30+ date columns
spread across 2-5 basis groups (Mgt acc / Audited / Indicative adjusted / ...)
with only a text label distinguishing them and no structural hint (no merged
cells) -- a real, high-risk correctness gap: get the wrong group and every
cited figure in the report is silently wrong.

Fixtures below use synthetic data only, never real client databooks (see
AGENT_UPGRADE_NOTES.md: never commit client data into this repo).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.tools import ToolRegistry
from coding_agent.tools import databook_metrics as dm

# ---------------------------------------------------------------------------
# canonical_stage_label
# ---------------------------------------------------------------------------

class TestCanonicalStageLabel:
    def test_plain_english_variants(self) -> None:
        assert dm.canonical_stage_label("Indicative adjusted") == "Indicative adjusted"
        assert dm.canonical_stage_label("Mgt acc") == "Mgt acc"
        assert dm.canonical_stage_label("Audited") == "Audited"
        assert dm.canonical_stage_label("Audit adjustment") == "Audit adjustment"
        assert dm.canonical_stage_label("Indicative adjustment") == "Indicative adjustment"

    def test_typo_variant(self) -> None:
        assert dm.canonical_stage_label("Indivative Adjusted") == "Indicative adjusted"

    def test_traditional_and_simplified_chinese(self) -> None:
        assert dm.canonical_stage_label("示意性調整後") == "Indicative adjusted"
        assert dm.canonical_stage_label("示意性调整后") == "Indicative adjusted"
        assert dm.canonical_stage_label("審定數") == "Audited"
        assert dm.canonical_stage_label("审定数") == "Audited"

    def test_client_specific_indicative_adjusted_variant(self) -> None:
        """示意性调整数 ("indicative adjustment FIGURE") is a client-specific
        way of writing 示意性调整后 -- the 数 suffix mirrors 审定数 (the final
        audited FIGURE) rather than meaning "the adjustment amount itself".
        Must resolve to the FINAL-BALANCE stage, not the delta stage."""
        assert dm.canonical_stage_label("示意性调整数") == "Indicative adjusted"
        assert dm.canonical_stage_label("示意性調整數") == "Indicative adjusted"

    def test_priority_ordering_adjusted_vs_adjustment(self) -> None:
        """"Indicative adjustment" (the delta) must not be confused with
        "Indicative adjusted" (the final balance) -- these are DIFFERENT
        canonical stages the reference project treats distinctly."""
        assert dm.canonical_stage_label("Indicative adjustment") == "Indicative adjustment"
        assert dm.canonical_stage_label("示意性調整") == "Indicative adjustment"
        assert dm.canonical_stage_label("Indicative adjusted") != dm.canonical_stage_label("Indicative adjustment")

    def test_unrelated_text_returns_none(self) -> None:
        assert dm.canonical_stage_label("Cash at bank") is None
        assert dm.canonical_stage_label("") is None
        assert dm.canonical_stage_label(None) is None

    def test_overly_long_cell_is_not_a_label(self) -> None:
        # A long free-text remark that happens to contain a variant substring
        # should not be treated as a basis-label cell.
        long_text = "This account represents mgt acc figures across many detailed sub-items " * 3
        assert dm.canonical_stage_label(long_text) is None


class TestUnitMarker:
    def test_recognizes_common_markers(self) -> None:
        assert dm.contains_unit_marker("CNY'000") == "CNY'000"
        assert dm.contains_unit_marker("人民币千元") == "人民币千元"
        assert dm.contains_unit_marker("some text CNY'000 more text") == "CNY'000"

    def test_no_marker_returns_none(self) -> None:
        assert dm.contains_unit_marker("Cash at bank") is None
        assert dm.contains_unit_marker(None) is None


# ---------------------------------------------------------------------------
# locate_stage_columns
# ---------------------------------------------------------------------------

def _synthetic_databook_grid() -> list[list]:
    """Mirrors the real structure found in production databooks: a section
    label row, a date row (dates repeated once per basis group), and a basis
    row -- Mgt acc / Audited / Indicative adjusted columns side by side with
    no structural hint distinguishing them beyond the text label."""
    return [
        [None, "Indicative adj", None, None, None, None, None, None],
        [None, "CNY'000", "2019-12-31", "2020-12-31", "2021-12-31", "2019-12-31", "2020-12-31", "2021-12-31"],
        [None, "CNY'000", "Mgt acc", "Mgt acc", "Mgt acc", "Indicative adjusted", "Indicative adjusted", "Indicative adjusted"],
        [None, "Cash at bank", "100", "200", "300", "110", "210", "310"],
    ]


class TestLocateStageColumns:
    def test_finds_the_basis_row_not_the_section_label_row(self) -> None:
        rows = _synthetic_databook_grid()
        result = dm.locate_stage_columns(rows)
        assert result["found"] is True
        assert result["basis_row_idx"] == 2  # not row 0, the mere section-title row

    def test_separates_indicative_adjusted_from_mgt_acc(self) -> None:
        rows = _synthetic_databook_grid()
        result = dm.locate_stage_columns(rows)
        indicative_cols = {e["col_idx"] for e in result["stage_columns"]["Indicative adjusted"]}
        mgt_acc_cols = {e["col_idx"] for e in result["stage_columns"]["Mgt acc"]}
        assert indicative_cols == {5, 6, 7}
        assert mgt_acc_cols == {2, 3, 4}
        assert indicative_cols.isdisjoint(mgt_acc_cols)

    def test_associates_correct_dates_per_column(self) -> None:
        rows = _synthetic_databook_grid()
        result = dm.locate_stage_columns(rows)
        by_col = {e["col_idx"]: e.get("date") for e in result["stage_columns"]["Indicative adjusted"]}
        assert by_col[5] == "2019-12-31"
        assert by_col[6] == "2020-12-31"
        assert by_col[7] == "2021-12-31"

    def test_detects_unit_marker(self) -> None:
        rows = _synthetic_databook_grid()
        result = dm.locate_stage_columns(rows)
        assert result["unit_marker"] == "CNY'000"

    def test_recovers_excel_serial_date_alongside_real_datetimes(self) -> None:
        """Some cells in real databooks hold a genuine date whose number
        format didn't survive -- openpyxl then returns the raw serial float
        instead of a datetime, observed directly in a real sample databook
        alongside correctly-typed datetime cells in the SAME row."""
        rows = [
            [None, "CNY'000", dt.date(2019, 12, 31), 43830, "2021-12-31"],
            [None, "CNY'000", "Mgt acc", "Indicative adjusted", "Audited"],
        ]
        result = dm.locate_stage_columns(rows)
        by_col = {e["col_idx"]: e.get("date") for stage in result["stage_columns"].values() for e in stage}
        assert by_col[2] == "2019-12-31"  # real datetime.date
        assert by_col[3] == "2019-12-31"  # raw Excel serial number 43830
        assert by_col[4] == "2021-12-31"  # plain ISO string

    def test_no_stage_row_found_returns_found_false(self) -> None:
        rows = [
            ["Cash at bank", "100", "200"],
            ["Accounts receivable", "50", "60"],
        ]
        result = dm.locate_stage_columns(rows)
        assert result == {"found": False}

    def test_single_matching_cell_is_not_enough(self) -> None:
        """A single stray cell that happens to match a label isn't a basis
        row on its own -- avoids false-positives on ordinary account rows
        that merely mention e.g. "audited" in passing."""
        rows = [["Note: audited by XYZ", "100", "200"]]
        result = dm.locate_stage_columns(rows)
        assert result["found"] is False

    def test_picks_the_densest_row_when_multiple_rows_partially_match(self) -> None:
        rows = [
            [None, "Indicative adjusted", None],  # 1 match -- section title, not the real basis row
            ["Mgt acc", "Audited", "Indicative adjusted"],  # 3 matches -- the real basis row
        ]
        result = dm.locate_stage_columns(rows)
        assert result["basis_row_idx"] == 1

    def test_financial_expenses_style_sheet_with_five_distinct_stages(self) -> None:
        """Mirrors a real second sheet shape where Audit adjustment and
        Indicative adjustment (deltas) sit alongside the three base stages --
        five canonical stages must all resolve distinctly."""
        rows = [
            ["Mgt acc", "Audit adjustment", "Audited", "Indicative adjustment", "Indicative adjusted"],
        ]
        result = dm.locate_stage_columns(rows)
        assert result["found"] is True
        assert set(result["stage_columns"].keys()) == {
            "Mgt acc", "Audit adjustment", "Audited", "Indicative adjustment", "Indicative adjusted",
        }


# ---------------------------------------------------------------------------
# Tool-level integration (real .xlsx file, synthetic data only)
# ---------------------------------------------------------------------------

@pytest.fixture
def strong_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="k", model="gpt-test", intelligence_tier="strong"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


class TestLocateDatabookStageColumnsTool:
    def test_end_to_end_via_tool_registry(self, tmp_path: Path, strong_config: AppConfig) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Financials"
        grid = _synthetic_databook_grid()
        for row in grid:
            ws.append(row)
        xlsx_path = tmp_path / "synthetic_databook.xlsx"
        wb.save(str(xlsx_path))

        registry = ToolRegistry(tmp_path, strong_config)
        out = registry.execute("locate_databook_stage_columns", {"path": "synthetic_databook.xlsx", "sheet": "Financials"})

        import json
        result = json.loads(out)
        assert result["found"] is True
        assert result["sheet"] == "Financials"
        indicative_cols = {e["col_idx"] for e in result["stage_columns"]["Indicative adjusted"]}
        assert indicative_cols == {5, 6, 7}

    def test_missing_openpyxl_fails_cleanly(self, tmp_path: Path, strong_config: AppConfig, monkeypatch) -> None:
        registry = ToolRegistry(tmp_path, strong_config)
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        import builtins
        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError, match="openpyxl"):
            registry.execute("locate_databook_stage_columns", {"path": "x.xlsx"})
