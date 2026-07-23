# Workbook spec schema (JSON)

`scripts/build_workbook.py` reads one JSON file and writes the workbook. This is
the contract. Author the spec; let the script own every formatting decision.

```jsonc
{
  "workbook": {
    "project":  "Project Falcon",                 // deal/code name for the title block; optional
    "entity":   "Mox Bank Limited",               // legal entity
    "currency": "HK$'000",                         // as reported; drives the units sub-label
    "period":   "Year ended 31 December 2025",
    "source":   "Audited financial statements FY2025 (filename.pdf)"
  },
  "tables": [ /* one object per output tab, in the order they should appear */ ]
}
```

## Table object

```jsonc
{
  "sheet_name": "P&L",             // tab name, <=31 chars, unique (auto-truncated/deduped)
  "title": "Statement of profit or loss and other comprehensive income",
  "note_ref": "",                  // "" for primary statements; "Note 22" for note tables
  "page": 8,                        // PRINTED page in the source (for the index), not the PDF page
  "periods": ["2025", "2024"],     // one header per value column, left->right as printed
  "show_note_col": true,            // true shows a "Notes" column between label and values
  "rows": [ /* row objects, top to bottom */ ],
  "checks": [ /* optional foot/cross-cast checks */ ]
}
```

For a balance-sheet-style note with two dates use `["2025", "2024"]`; for a
matrix (e.g. changes in equity) use each column head, e.g.
`["Share capital","Accumulated losses","Reserves","FVOCI instruments","Other reserve","Total"]`.

## Row object

| field    | meaning |
|----------|---------|
| `label`  | line description shown in column B |
| `note`   | note reference shown in the Notes column (only if `show_note_col`) |
| `values` | array of numbers, one per period column. Use real negatives for bracketed figures; use `0` for a printed dash/nil |
| `type`   | `data` (default) \| `subtotal` \| `total` \| `header` \| `blank` |
| `id`     | short unique tag; REQUIRED if any `sum`/`sum_range`/`check` references this row |
| `indent` | optional integer to indent the label (nested items) |
| `sum`    | on a subtotal/total: list of row `id`s to add; prefix an id with `-` to subtract, e.g. `["ta","-tl"]` for Net assets = Total assets − Total liabilities |
| `sum_range` | on a subtotal/total: `["firstId","lastId"]` → `=SUM()` over that contiguous block (do not include an intermediate subtotal inside the block, or it double-counts) |

Row-type behaviour:
- `data` — plain line; supply `values`.
- `subtotal` — bold, thin rule above the row; value = its `sum`/`sum_range` (or `values` if given).
- `total` — bold, grey fill, medium bottom border; value = its `sum`/`sum_range` (or `values`).
- `header` — bold section heading, no numbers (e.g. "Assets", "Operating activities").
- `blank` — spacer row.

Prefer `sum`/`sum_range` over hard-coded `values` on subtotal and total rows, so
the tab foots with live formulas. Keep the reported total for the `checks` block.

## Checks (per table)

Each check writes a live formula `= <built cell> − <printed figure>` that must
evaluate to nil. This is the self-proof that extraction is correct.

```jsonc
"checks": [
  {"label": "Total operating income foots", "of": "toi", "equals": 719056},
  {"label": "Closing total equity foots",   "of": "c25", "col": 5, "equals": 2613897}
]
```

- `of` — the `id` of the subtotal/total to test.
- `equals` — the figure as PRINTED in the statement (key it independently; do not
  copy from your own `sum`).
- `col` — which value column to test (0-based; default 0 = first period). Use it
  to check the "Total" column of a matrix.

Always give every statement at least one check on its bottom-line total, and add
a cross-cast check where the statement asserts one (e.g. Net assets = Total
equity; closing cash = note on cash and cash equivalents).

See `assets/example_spec.json` for a complete, real example (Mox Bank FY25: the
four primary statements plus every numeric note table, 37 tabs).
