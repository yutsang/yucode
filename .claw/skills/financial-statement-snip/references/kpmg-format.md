# KPMG house-format constants

These are baked into `scripts/build_workbook.py`, measured from the client
template. Listed here so the format can be verified or tuned in one place — you do
not need to apply any of this by hand; the script does.

| Element | Value |
|---|---|
| Body font | Arial 8 |
| Title font | KPMG Bold 18 (falls back to Arial Bold if the font isn't installed) |
| Navy (table-title band, index header, accents) | `#0C233C` |
| Light blue (column-header band) | `#76D2FF` |
| Grey (Total row fill) | `#E5E5E5` |
| Header text on bands | white, bold |
| Number format (value cells) | `_(* #,##0_);_(* (#,##0);_(* "-"??_);_(@_)` — thousands separator, negatives in brackets, nil as `-` |
| Row height — entity title | 22.8 |
| Row height — navy band | 19.5 |
| Row height — data rows | 12.0 |
| Gridlines | hidden on every sheet |
| Print | fit to one page wide, portrait |

Structural rules the script enforces:
- Column A is a narrow (3.4) left margin; labels live in column B.
- Each table tab: row 1 navy title band, row 2 the light-blue sub-header band
  directly beneath it, then data from row 3 (panes frozen at B3). The units label
  (e.g. "Amounts in HK$'000") sits in the leftmost cell of the light-blue
  sub-header (B2), not on a separate line.
- `subtotal` rows: bold, thin rule above.
- `total` rows: bold, grey fill, thin top + medium bottom border.
- A "Checks (should be nil)" block sits below each table; every check is a live
  `= built − printed` formula that must show `-` (nil).
- Index tab: title block (project, entity, period, currency, source) then a navy
  header row and one hyperlinked line per tab (name → tab, note ref, printed page,
  full statement title).

To adjust a colour or the number format for a different house style, edit the
constants block at the top of `build_workbook.py`; nothing else needs to change.
