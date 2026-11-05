# Journal

One file per stretch of work, recording what was done and why, in the order it
happened. It is not a changelog and not a specification.

The other documents answer different questions, and the split matters when you
are looking for something:

| Looking for | Read |
|---|---|
| What to build next, and in what order | `../REWRITE_PLAN.md` |
| What the Python did | `../PYTHON_IMPLEMENTATION_SUMMARY.md` |
| How the pieces fit, and which contracts are frozen | `../ARCHITECTURE.md` |
| Measured behaviour the port is checked against | `../../tests/golden/` |
| Why a thing ended up the way it did; what was tried and dropped; what a decision cost | here |

The journal exists because none of the others keep the reasoning once the
outcome is written down. A plan says "hide the whole hidden-column range"; the
journal says the fixture had to move to its own sheet first, because twelve
cases were quietly inheriting the quirk and a manifest note claimed only one
was affected.

## Entries

| File | Period | Covers |
|---|---|---|
| `2026-09_M0-WP0.md` | 2026-09-12 to 09-20 | M0 (freeze the Python, capture the oracle, reset the tree), three review rounds, WP0 (the TypeScript scaffold) |
| `2026-10-26_review.md` | 2026-10-26 | Re-run M0/WP0 checks, repair native-file scanner blind spots, and record remaining verification gates |
