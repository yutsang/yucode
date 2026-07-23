# Place the renderer here

Copy these two files from the original Claude skill package into this skill
directory (they are pure Python / JSON — no executables):

- `scripts/build_workbook.py`  (the deterministic KPMG-format renderer; openpyxl)
- `assets/example_spec.json`   (complete 37-tab example spec)

Until `build_workbook.py` is present, the snip workflow can extract and author
the spec but cannot render the workbook (step 6 of SKILL.md).
