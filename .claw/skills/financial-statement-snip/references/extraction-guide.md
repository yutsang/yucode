# Extraction guide

The goal is a faithful, footing snip: every figure exactly as printed, every subtotal
rebuilt as a formula where possible, and every period column preserved.

## 1. Read text layer first; flag image-only pages

Call `read_pdf_text` with `layout: true` (whitespace preserves printed column
positions). If the text layer is sparse or columns collapse on a page, this
environment cannot reliably read page images — record the page in the final
"unreadable pages" list rather than guessing. Never silently drop image-only pages.

## 2. Map printed pages to PDF pages

The Index uses printed page numbers. Record the printed page number in each table
spec; the coordinate tools take the PDF page number — keep both straight.

## 3. Scope

Always include the four primary statements, then every note containing a numeric
table. A note with multiple distinct tables produces multiple tabs.

## 4. Row classification

Classify each line as data, subtotal, total, header, or blank. Give any row
referenced by a formula or check a unique `id`.

## 5. Signs and nil

Bracketed figures are real negatives. Printed dashes are zero, but only for the
column band where the dash appears. (`extract_pdf_period_table` applies both rules
mechanically.)

## 6. Bilingual side-by-side extraction protocol

Mandatory for bilingual statements where one language is printed on the left and
another on the right.

### Why token-order extraction fails

In a bilingual note table the visual structure is commonly:

`left label | 2025 value | 2024 value | right translated label`

PDF text extraction may output words in a mixed order, split wrapped labels across
lines, and interleave translated labels with the numeric columns. Reading numeric
tokens left-to-right can assign a 2025 value to 2024, invent generic `Col 1`/`Col 2`
columns, mistake note numbers or percentages for values, and lose dashes.

### Required method

Call `extract_pdf_period_table` with the PDF page and (if known) the expected
periods. It implements the full protocol:

1. words extracted with coordinates (pdfplumber);
2. period headers detected by x-centre;
3. a column band per header, split at the midpoints between adjacent headers,
   outer edges extended by a conservative margin;
4. words grouped into visual rows by y-coordinate;
5. per row: label from the left area, values by band membership of each word's
   x-centre, dashes as 0 only inside a band, right-language labels kept in
   `right_label` and never counted as values;
6. wrapped label lines stitched onto the adjacent valued row (closer neighbour
   wins); standalone headings come back in `skipped_label_rows`;
7. `null` in a values slot means NOTHING was printed in that band — resolve or
   flag it; do not treat it as zero.

For matrix tables with TEXT column headers (changes in equity), use
`extract_pdf_words` and apply the same banding by the printed header x-centres.
Never infer a column from token position when header x-positions are available.

### Example spot-check: cash and cash equivalents note

For a page showing note `4. Cash and cash equivalents - net` with year headers
`2025` and `2024`, the following rows must map as:

| Row label | 2025 | 2024 |
| --- | ---: | ---: |
| Kas / Cash | 1,410 | 25,641 |
| PT Bank HSBC Indonesia - Rupiah bank | 17,948,575 | 4,697,031 |
| PT Bank CIMB Niaga, Tbk. - Rupiah bank | 24,694 | 19,606 |
| PT Bank HSBC Indonesia - US Dollar bank | 61,353,169 | 43,112,278 |
| PT Bank Maybank Indonesia, Tbk. - Rupiah deposit | 92,000,000 | 100,000,000 |
| PT Bank HSBC Indonesia - Rupiah deposit | 36,000,000 | 61,000,000 |
| PT Bank OCBC NISP, Tbk. - Rupiah deposit | 0 | 47,000,000 |
| PT Bank HSBC Indonesia - US Dollar deposit | 25,012,500 | 0 |
| Total before allowance | 232,340,348 | 255,854,556 |
| Allowance for impairment losses | (4,983) | (8,741) |
| Cash and cash equivalents - net | 232,335,365 | 255,845,815 |

This table is a regression test. A snip fails QA if any of these values lands under
the wrong year or if the headers are not `2025` / `2024`. (The same mapping rules
are unit-tested against a synthetic bilingual page in `tests/test_pdf_tables.py`.)

## 7. Multi-column matrices

For changes in equity and movement tables, detect all column headers by
x-coordinate (`extract_pdf_words`). Use each printed header as a period. Do not
create generic columns unless the source truly lacks column labels.

## 8. QA gates before delivery

Before delivering the workbook:
- search the generated spec for `Col 1`, `Col 2`, etc.; if printed years exist, fix;
- verify every primary statement has at least one nil check;
- spot-check one high-risk note table against the layout text from step 1;
- confirm all check cells recalculate to nil; and
- list any pages or tables that could not be fully parsed (no text layer,
  `found: false`, or unresolved `null` value slots).
