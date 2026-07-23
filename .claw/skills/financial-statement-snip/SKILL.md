---
name: financial-statement-snip
description: Extract ("snip") financial data from audited financial statements or annual reports (PDF) into one KPMG-house-format Excel workbook — Index tab plus one tab per primary statement and per numeric note table, with live footing formulas and nil checks. Use when the user asks to convert, snip, extract, digitise, or put financial statements into Excel. Column-safe for bilingual statements (left-language label / year value columns / right-language label).
---

# Financial Statement Snip

Turn a PDF of audited financial statements into a clean, footing Excel workbook in
KPMG house format. The workbook renderer is deterministic; the extraction work must
faithfully read the numbers and preserve the source period columns exactly.

## Setup (once per machine)

This skill directory must contain, alongside this file:
- `scripts/build_workbook.py` — the deterministic KPMG-format renderer
- `assets/example_spec.json` — a complete real example spec (37 tabs)

Both come from the original skill package — copy them in if `scripts/` is empty.
Everything runs on pure Python (openpyxl + pdfplumber, both already installed);
no pdftotext/poppler or any other executable is needed or allowed.

## What you produce

One .xlsx with:
- an **Index** tab (project/entity/period/currency/source header, then a hyperlinked
  row per table with note reference and printed page), and
- **one tab per table** — the four primary statements plus every numeric note table —
  each in KPMG format with live subtotal/total formulas and a nil-check block.

## Workflow

1. **Read the source.** Call `read_pdf_text` with `layout: true` (this is the
   pdftotext -layout equivalent; plain mode loses column alignment). Confirm all
   four primary statements and every numeric note table are present in the text
   layer. If a statement or note page has NO text layer, do not guess: record the
   page in the final "unreadable pages" list — this environment cannot reliably
   read page images.

2. **Detect table layout before extracting values.** For each table, identify:
   - printed page number (for the Index — not the PDF page number);
   - title / note number;
   - value columns and their headers; and
   - whether the page is bilingual side-by-side.

3. **Extract values with the coordinate tools — never by reading order.**
   - `extract_pdf_period_table` (path, page, periods) is the primary tool: it
     detects year headers by x-centre, builds column bands, groups rows by
     y-coordinate, maps every value to the band its x-centre falls in, treats
     dashes as 0 only inside a band, returns bracketed figures as negatives,
     stitches wrapped labels, and keeps right-language translated labels
     separate. **Mandatory for every bilingual side-by-side note table.**
   - `extract_pdf_words` (raw words + coordinates) is the fallback for unusual
     layouts and matrix tables with TEXT column headers (e.g. changes in
     equity) — apply the same banding logic using the printed header x-centres.
   - The layout text from step 1 is for orientation and labels; the numbers in
     the spec must come from the coordinate tools.
   - Details and the regression example: `references/extraction-guide.md`.

4. **Scope: every numeric note table by default.** The four primary statements
   AND every note containing a numeric table, walked in order. A note with
   several sub-tables becomes several tabs. Skip only narrative-only notes.

5. **Build the spec.** Author one JSON spec (schema: `references/spec-schema.md`)
   describing the workbook and every table. Classify rows as
   data / subtotal / total / header / blank. Bracketed figures are real
   negatives; printed dashes are 0. Rebuild subtotals and totals as `sum` /
   `sum_range` formulas when the components are identifiable. Add checks for
   bottom-line totals and cross-casts, keying `equals` from the PRINTED figure.

6. **Render the workbook.** Via the bash tool, from this skill's directory:
   `python scripts/build_workbook.py spec.json "Entity FY25 - financial statements.xlsx"`
   (KPMG format constants are baked into the script — `references/kpmg-format.md`
   documents them; never apply formatting by hand.)

7. **Prove it foots.** Recalculate and confirm every check cell is nil. If any
   check is not nil, fix the spec; do not suppress the check.

8. **Fail closed on unsafe extraction.** Do not deliver a workbook if:
   - a table has generic headers like `Col 1`, `Col 2` when year headers are printed;
   - a row has values but no value is assigned to a named period
     (`extract_pdf_period_table` returns `null` for an unassigned band — resolve
     or flag it, never silently drop it);
   - a bilingual table was parsed by token order rather than the coordinate tools; or
   - key rows fail a source spot-check against the layout text.

## Bilingual note table hard rule

When a source page visually shows columns headed 2025 and 2024, the Excel periods
must be exactly `2025` and `2024`, and each value must be placed in the column whose
horizontal x-position corresponds to that header. For example, in a cash and cash
equivalents note, a row showing `Kas / Cash` with `1,410` under 2025 and `25,641`
under 2024 must be extracted as `[1410, 25641]`, never `[25641, 1410]`, and never
into unnamed columns. `extract_pdf_period_table` enforces this mechanically — use it.

## Constraints

- Snip figures exactly as printed — do not reclassify, restate, rescale, or tidy.
- Keep the reported currency and units.
- Preserve printed period order.
- Treat dashes as zero only in the column band where the dash is printed.
- If a table is unreadable or column bands cannot be determined
  (`extract_pdf_period_table` returns `found: false`), flag it instead of guessing.
