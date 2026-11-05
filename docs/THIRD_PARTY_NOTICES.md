# Third-party notices

yucode is proprietary (see `LICENSE`). Code copied from an open-source project
keeps its licence header in the file that carries it, and gets a row here
saying where it came from. Anything from a proprietary source, or from leaked
source of any kind, is not admissible — see `docs/REWRITE_PLAN.md` §2
("Licensing rule") and §7 rule 8.

## Copied source

| File in this repo | Upstream | Licence |
|---|---|---|
| _(none yet)_ | | |

## Vendored assets

| Asset | Upstream | Licence |
|---|---|---|
| `tests/golden/fonts/YucodeGoldenLatin-Regular.ttf` | Noto Sans, subset and renamed — see `tests/golden/fonts/SOURCES.json` | SIL OFL 1.1 (`OFL-NotoSans.txt`) |
| `tests/golden/fonts/YucodeGoldenCJK-Regular.ttf` | Noto Sans SC, instanced at wght 400, subset and renamed | SIL OFL 1.1 (`OFL-NotoSansSC.txt`) |

## Runtime dependencies

Listed once `package.json` has any. Every entry must be pure JavaScript or
WebAssembly; `scripts/check-no-binaries.mjs` enforces it on every install.
