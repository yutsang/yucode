# yucode → TypeScript rewrite: implementation plan

Written 2026-09-12 for the owner and for the implementing agents (Opus). Every
work package (WP) below is meant to be handed to one agent session on its own.
Read §0–§4, §7 and the corrections section directly below before taking any WP.

---

## 0.0 Corrections found during M0 verification (2026-09-12)

The plan below was written from a reading session that did not run the code.
Sixteen of its claims were checked against the tree and the machine during
M0 and did not hold. **Where this section and the plan body disagree, this
section wins.** The plan body is left unedited so the reasoning behind each
work package stays readable.

### Changes an acceptance criterion depends on

1. **`python -m coding_agent` does not work.** There is no
   `coding_agent/__main__.py`. The working invocations are
   `python -m coding_agent.interface.cli` (guard at `cli.py:2417`) or the
   console script `yucode` (`pyproject.toml:60`). Every command line in this
   plan and in the docs it produces must use one of those.
2. **`show-config` has no `--output-format` flag.** It is registered with no
   arguments at all (`cli.py:75`); `handle_show_config` (`cli.py:519-522`)
   prints the raw settings file. §4 step 4 and WP1's acceptance criterion
   therefore go through
   `load_app_config(path, workspace).to_control_dict()` (`settings.py:319`)
   instead. `status --output-format json` is the closest CLI equivalent but
   covers only 9 keys (`cli.py:2168-2178`).
3. **`doctor` is not offline.** It issues live provider requests — a
   non-streaming probe, a streaming probe and a tool-calling probe
   (`cli.py:2277-2312`). Budget for it when capturing baselines.
4. **Four `diagnose_env.py` check names in §A.6 are wrong.** The registered
   names are `git_context_resilience`, `tls_context_build`,
   `mcp_timeout_behavior` and `prompt_toolkit_windows_input`. The count of 15
   is correct. The `@check("name")` string is authoritative — several
   functions are named differently from the check they register
   (decorator at `diagnose_env.py:51-62`).
5. **WP-D0 is sized M in §5 and is not an M.** The real shape is ~1,900 LOC
   across four modules, ~60 KB of committed fixtures and two vendored font
   subsets, split over three commits. It is the largest single work package
   in M0.

### Two findings that change the M3 design

6. **Pillow measures hinted, grid-fit advances, and a TypeScript port cannot
   compute them.** This machine has `raqm=False`, so Pillow runs
   `Layout.BASIC` and `getlength` loads glyphs with `FT_LOAD_DEFAULT` — every
   advance is grid-fit to a whole pixel by the TrueType interpreter. Measured
   on Arial at 9 pt, **21 of the 95 printable ASCII glyphs** have a hinted
   advance that `round(hmtx advance)` gets wrong: `C D F H J M N R T U W Z c
   k m s v w x y z`. §3 of M3 assumed `fonts.ts` could reproduce the FreeType
   integer pipeline from fontkit's font-unit advances. It cannot.
   `glyph_advances_px` in the golden manifest is the only transferable
   artefact, and `fonts.ts` must ship it as a lookup table.

   Two qualifications, both measured during WP-D0 and both worth knowing
   before reading the goldens. The divergence is a property of the specific
   font, not of hinting in general: the vendored Noto Sans subset the
   `portable` cases use has **zero** mismatches across the same 95 glyphs, so
   the portable pass alone would not catch a port that computes advances by
   rounding — the `system` (Arial) cases are the ones that would, and Arial is
   what the target box actually resolves. And CJK is clean either way (STHeiti
   and the vendored Noto Sans SC subset: zero mismatches over 37 glyphs),
   because CJK advances are uniform-width and land on integers.
7. **`databook.xlsx` cannot be built with openpyxl.** Four of the features
   WP-D0 requires are unreachable through it: a formula's cached `<v>` is
   written as an empty element; `CellRichText` round-trips as `t="inlineStr"`
   and never reaches `xl/sharedStrings.xml` (openpyxl emits no shared-strings
   part by default); floats go through `"%.16g"` so `0.30000000000000004` is
   unreachable; and `column_dimensions.group('C','E',hidden=True)` also
   injects `outlineLevel="1"`. The fixture is hand-assembled raw OOXML,
   following the precedent already in the repo for PDFs
   (`tests/test_pdf_tables.py:23-53`).

### Smaller things that would have tripped an implementing session

8. **`v0.6.0` was pushed to `origin`** — §9 Q2 assumed it was local-only.
   Owner's decision: leave every pushed tag alone; the TS line starts at
   `1.0.0`. §9 Q2 is closed.
9. **`build_workbook.py` and `assets/example_spec.json` do not exist on this
   machine** (`find /` over the whole filesystem). Owner's decision: WP-D13
   is written from `references/spec-schema.md` + `references/house-format.md`
   as structural assertions plus one hand-checked example spec; WP-D0
   captures no goldens for it. §9 Q4 is closed.
10. **The Chinese 實戰架構與安全指南 is not tracked by git** — `.gitignore`
    carries `Claude Code Coding Agent*.md`. §4 step 5 moves it to
    `docs/legacy/`, which first requires relaxing that ignore rule.
11. **`.github/workflows/release.yml` is also Python-specific** and triggers
    on `v*` tags. §5 WP0 mentions only `ci.yml`; both need rewriting.
    (`python-final` does not match `v*`, so tagging in M0 triggers nothing.)
12. **`diagnose_agent_trace.json` is gitignored by name.** Baseline traces
    must be written under a different filename to be committable.
13. **The eval suite's suggested workspace `~/Downloads/kpi-main` does not
    exist.** M0 uses `/path/to/sample-workspace` (HEAD `99918be`,
    clean tree) and records that SHA alongside the baseline.
14. **Node on the Mac is `/usr/local/bin/node` v21.7.1**, a 2024 `.pkg`
    install that shadows Homebrew's 20.2.0. `brew install node@24` yields
    24.20.0 (keg-only). Owner's decision: leave it until WP0 needs it; the
    PATH shadowing has to be resolved then, not just the install.

15. **The read-only bash relaxation is dead code.** WP5 says to port
    `_READ_ONLY_COMMANDS` "incl. cmd.exe builtins". Those lists are consulted
    only by `PermissionEnforcer.check_bash()`, which nothing in
    `coding_agent/` calls; `_execute_tool` goes through `check()`, and `bash`
    is registered as `danger-full-access`, so read-only mode denies every
    bash command. `pure_functions.json` records both paths. WP5 must wire the
    relaxation in deliberately or drop it — not port it as if it worked.
16. **`rm -rf ~` without a trailing slash is not blocked.** The pattern at
    `safety.py:33` requires a following space or `/`, so `rm -rf ~/` is
    critical and a bare `rm -rf ~` at end of string is only the generic
    "recursive delete" warning. Recorded as-is in the golden; the port should
    close the gap and note the deviation.

### Environment confirmed present for WP-D0 (2026-09-12, this Mac)

openpyxl 3.1.2 · python-pptx 1.0.2 · python-docx 1.1.0 · pdfplumber 0.11.9 ·
Pillow 10.4.0 with `raqm=False` · lxml 6.1.0 · fontTools 4.58.4 ·
`pyftsubset` · `fc-match`. `reportlab` and PyMuPDF are absent and not needed —
the repo's raw-bytes PDF builder covers every PDF fixture.

Font resolution as it actually happens here:
`resolve_font_path("Arial", is_cjk=False)` →
`/System/Library/Fonts/Supplemental/Arial.ttf`;
`resolve_font_path("Microsoft YaHei", is_cjk=True)` →
`/System/Library/Fonts/STHeiti Light.ttc` (both YaHei paths and
`PingFang.ttc` are absent, so the chain falls through `text_metrics.py:65-66`).

---

## 0. Context

yucode is a Python coding-agent harness (23,244 LOC in `coding_agent/`, 981
test functions) plus a firm-specific domain layer (Excel / Word / PPTX / PDF /
FDD-databook tools, ~3,000 LOC). It runs on a locked-down corporate Windows 11
box with no admin rights, where any freshly created unsigned `.exe`/`.node` is
flagged by endpoint protection. Until now the box only had Python, which is
why the whole thing is stdlib-only Python invoked via `python -m`.

Two things changed:

1. **The box now has Node 24**, and the owner wants the harness rewritten in
   TypeScript so it can borrow directly from the open-source TypeScript agents
   (Gemini CLI, cline SDK, pi, deepseek-harness) instead of transliterating
   Rust designs by hand.
2. **Python will be removed from the box later.** So the domain layer cannot
   stay in Python behind the existing MCP bridge; it has to be ported too.

The rewrite is also the moment to shed what the 2026-08 audit found dead
(≈1,000 LOC of never-executed registries and stubs) and to adopt the one
architectural change that audit wanted but declined to retrofit: an
append-only session event log.

**Target model:** DeepSeek V4 Flash through an OpenAI-compatible gateway on
the company box (per 2026-08-26); the Azure/APIM-style GPT-5.5 gateway remains
a supported configuration. Both are served by one provider class.

**Outcome:** a `yucode` that lands on the box as an unzipped folder (built,
scanned and bundled with its pure-JS `node_modules` on the Mac; `npm ci
--ignore-scripts` is the fallback), runs as `node dist/cli/main.js` via a
plain-text `yucode.cmd`, reproduces the Python harness's measured behaviour
(behavioural diagnostic 25/25, eval suite parity), and carries the two
production skills with at most a one-line change.

---

## 1. Decisions already taken by the owner (2026-09-12)

| Decision | Consequence |
|---|---|
| **Node 24**, and the "no new `.exe` / `.node` anywhere" rule still applies | Toolchain is `tsc` + `node --test` only. No esbuild, vitest, tsx, bun, biome, oxlint, `@vscode/ripgrep`, keytar, node-pty, better-sqlite3. Every dependency must be pure JS or WASM. A CI check enforces it (§3.6). |
| **Domain layer must be fully ported to TS** (Python will not exist on the box) | Milestone M3 (§5) ports all 22 office tools plus `edit_notebook_cell` and adds a new `build_workbook`, with Python-captured golden fixtures as the oracle. The Python `office_mcp` stays usable only as a *transitional* MCP server on the Mac during M1–M2. |
| **Same repo; summarise the Python, keep an untracked backup, reset the tree, start clean** | §4 cutover procedure. Git history and tags are kept; the Python tree is tagged `python-final` and copied to a gitignored `_legacy_python/` for the porting agents to read. |
| **v1 keeps:** Ink (React) full-screen TUI, VS Code JSONL bridge + HTTP/SSE server, `plugin.json` plugin system | These are milestones M2 and M4. |
| **v1 does NOT include the coordinator** (plan→research→work→validate) | v1 = single ReAct loop + `agent` sub-agent tool. Coordinator is v2 (§8), still "tune, don't remove". |
| **Keep git history; same `main`** | §4 step 5 is one commit on `main`, no orphan branch. |
| **Port `build_workbook.py` as a TS tool; change one line of the snip skill** | M3 gets WP-D13. The original script is not on this Mac (see D13), so it is re-authored from `references/spec-schema.md` + `house-format.md`. |
| **Fix both openpyxl quirks** in the port | Hidden column ranges hide `min..max`; `write_excel_cell` edits the sheet XML in place and keeps charts/pivots. Goldens for these two carry the *corrected* expectation, noted in `manifest.json`. |
| **Install on the box: prefer local/offline** ("盡量本地") | Primary deployment artefact is a zip of the repo **with `node_modules`** built and scanned on the Mac; `npm ci` on the box is the documented alternative. Pure-JS deps make the tree OS-portable, which is why this works. |

Assumptions the plan makes (flag at review if wrong):

- The owner's existing `~/.yucode/settings.yml` on the box must keep working
  unchanged → **config key names and file layout are frozen** (§3.3).
- `~/.yucode/memory/*.md`, `~/.yucode/trusted_dirs.json`, `.claw/skills/`,
  `AGENTS.md`/`CLAUDE.md` discovery all keep their formats.
- Saved Python sessions (`sessions/<id>.json`, version 1) do **not** need to
  be resumable from the TS version. (Cheap to add an importer later.)
- The Mac dev box gets Node 24 installed (it has 21.7.1 today).
- WASM in `node_modules` (Ink's yoga layout) is acceptable to the EDR. It is
  not a PE binary. Confirm on the box in M0 (§9).

---

## 2. What the reference projects actually are (verified against the repos, 2026-09-12)

The 2026-08 audit already established that blog posts about these projects
are unreliable. Everything below was read from the repositories.

| Project | Verdict for this rewrite |
|---|---|
| **claw-code** (`ultraworkers/claw-code`, Rust, MIT) | **Do not use as an architecture source.** Its README calls itself "closer to a museum exhibit than a product"; 48k Rust LOC generated by an agent swarm in 3 days (2026-03-31→04-03); parity measured against its own mock Anthropic service. yucode's dead registries were ported from it. |
| **grok-build** (`xai-org/grok-build`, Rust, Apache-2.0, one-way monorepo export) | Ideas already ported to Python in 2026-07 (pruning tiers, compaction prompt, background-task reminders, edit-recovery hints, trust gate). Nothing new to take beyond the tool-types / tool-protocol / tool-runtime crate split. |
| **OpenAI Codex** (`openai/codex`, Rust) | **The TypeScript CLI was deleted 2025-08-08** (commit `408c7ca14`); `codex-cli/` is now a launcher shim. Take: the `apply_patch` envelope (already in `tools/patch.py`), `docs/agents_md.md` discovery semantics, `docs/exec.md` headless contract, and the app-server ↔ TUI "UI is a protocol client" seam. |
| **Gemini CLI** (`google-gemini/gemini-cli`, TS, Apache-2.0, Node ≥20) | **Primary TS reference for tools, policy, config, TUI.** Copy: `packages/core/src/tools/tools.ts` (`DeclarativeTool` → `ToolInvocation` two-phase pattern, `shouldConfirmExecute`), the 5-layer settings precedence with a system-override file, the `ContentGenerator` decorator chain (`logging`/`recording`/`fake`) for record-replay tests, `packages/cli/src/ui/components/shared/text-buffer.ts` for multi-line input/paste, `"overrides": {"prebuild-install": "npm:nop@1.0.0"}`. **Excise:** vendored `rg-win32-x64.exe`, `@github/keytar`, `node-pty` (all present in its tree). |
| **cline SDK** (`cline/cline` → `sdk/packages/{shared,llms,agents,core}`, Apache-2.0) | **Primary reference for layering.** `sdk/ARCHITECTURE.md` states the rule this plan adopts: `@cline/agents` is the *stateless* loop (iteration, tool orchestration, event emission); `@cline/core` owns *all* state (sessions, storage, config watching). `@cline/core` has zero native deps. Read that doc first. |
| **pi** (`earendil-works/pi`, ex `badlogic/pi-mono`, TS, MIT, Node ≥22.19) | Copy its **supply-chain hardening verbatim** (`.npmrc` `save-exact`, `ignore-scripts`, shrinkwrap, lifecycle-script allowlist, `npm audit signatures`). Reducer-based session harness (`packages/agent/harness/*`) is a good shape reference. No permission system at all, so nothing to copy there. Ships one `.node` (clipboard) — skip. |
| **deepseek-harness** (`deepseek-ai/deepseek-harness`, TS, MIT, Node ^22.19 ∥ ≥24, 1 month old, "THERE WILL BE BREAKING CHANGES") | Mine for patterns, don't build on it (Cordis plugin runtime, `native/system` addon). Copy: `packages/core/agent-loop` contract (`maxParallelToolCalls`, exclusive-vs-parallel tool ordering, cancellation preserving already-streamed text), `session-persistence-jsonl` + versioned session format, first-class `tool-pwsh`, explicit Windows CI gates. |
| **opencode** (`anomalyco/opencode`, TS on Bun, MIT) | Distributed as a compiled Bun `.exe` — non-starter as a base. Two 2026 CVEs from its local-HTTP-server design (CVE-2026-22812 unauthenticated RCE, CVE-2026-22813 XSS→RCE) → our HTTP server binds loopback with a per-run bearer token from day one. Ideas worth keeping: Context Epoch (render system prompt once per epoch), shadow-git snapshots, compaction thresholds derived from the model window (`usable = limit - min(20000, max_output)`), one `.txt` prompt file per tool. |
| **Qwen Code** | Hard fork frozen at Gemini CLI 0.8.2. Go upstream instead. |
| **Roo Code** | Archived 2026-05-15. Ignore. |
| **`@anthropic-ai/claude-agent-sdk`** | No public source, proprietary licence, ships 8 platform binaries. **Do not copy from it, and do not copy from the leaked Claude Code source** the Chinese 實戰指南 cites. Use its public docs only for hook/permission vocabulary. |

**Licensing rule.** yucode is private/proprietary. Copying Apache-2.0 (Gemini
CLI, cline, codex, grok-build) or MIT (pi, dsh, opencode) code is fine
provided the licence header stays on the copied file and
`docs/THIRD_PARTY_NOTICES.md` lists file → source → licence (grok-build does
exactly this). Anything from a proprietary or leaked source is forbidden.

---

## 3. Target architecture

### 3.1 Layering (the rule that keeps the loop testable)

```
                 ┌────────────────────────────────────────────┐
  UI clients     │ cli (commander, headless) │ tui (Ink) │ bridge (JSONL) │ server (HTTP+SSE) │
                 └──────────────┬─────────────────────────────┘
                                │ consumes AgentEvent stream, calls runtime.run()
                 ┌──────────────▼─────────────────────────────┐
  stateful core  │ runtime/runtime.ts  — composes config, provider, tools, session store,
                 │ metrics, audit, background tasks; owns lifecycle and persistence
                 └──────────────┬─────────────────────────────┘
                                │
                 ┌──────────────▼─────────────────────────────┐
  stateless loop │ runtime/agent-loop.ts — runTurn(): prompt assembly → provider call →
                 │ tool dispatch pipeline → supervisors → reminders → yields AgentEvent
                 │ Touches no filesystem except through injected SessionLog / ToolContext
                 └──────────────┬─────────────────────────────┘
                                │
      provider/  tools/  security/  hooks/  mcp/  prompt/  compaction/  session/
```

Enforced with one ESLint `no-restricted-imports` rule: nothing under
`src/runtime/agent-loop.ts`, `src/tools`, `src/provider` may import from
`src/cli`, `src/tui`, `src/bridge`, `src/server`.

### 3.2 Repository layout (single npm package at the repo root)

```
package.json          name yucode, "type":"module", engines.node ">=24", no "bin" (see yucode.cmd)
tsconfig.json         module NodeNext, target ES2023, strict, erasableSyntaxOnly + verbatimModuleSyntax, outDir dist/
.npmrc                ignore-scripts=true, save-exact=true
yucode.cmd            @node "%~dp0dist\cli\main.js" %*      (plain text; the only launcher)
yucode                #!/bin/sh exec node "$(dirname "$0")/dist/cli/main.js" "$@"
config/default.yml    bundled defaults (same keys as today's coding_agent/config/config.yml)
src/
  config/     schema.ts (zod → AppConfig), load.ts (discovery + deep-merge + env), trust.ts, state-dir.ts
  provider/   types.ts, openai-compatible.ts, sse.ts, retry.ts, breaker.ts, self-heal.ts,
              text-tool-calls.ts, usage.ts, http.ts (undici dispatcher: CA bundle / verify_tls / proxy),
              decorators/{recording,mock,logging}.ts
  session/    events.ts, log.ts (JSONL append/read), projection.ts (deriveMessages + pruning),
              store.ts (list/latest/resume), tokens.ts (bytes/3.2 estimator), archive.ts
  tools/      define.ts (Tool contract), registry.ts, context.ts, path-boundary.ts, spill.ts, truncate.ts
              fs/  read-file, read-files, write-file, edit-file, glob, grep, list-dir, file-outline
              patch/apply-patch.ts     shell/bash.ts, background.ts
              web/fetch.ts, search.ts  misc/{todo,ask-user,load-skill,config-read,structured-output,mcp-resources}.ts
              memory/*.ts              agent/subagent.ts
              office/                  (M3 — see §5 M3)
  security/   permissions.ts, safety.ts, redact.ts, env-scrub.ts
  hooks/      runner.ts
  mcp/        client.ts, manager.ts, presets.ts, serve.ts (M3: expose office tools as an MCP server)
  prompt/     assembler.ts, sections/*.ts, instructions.ts, skills.ts, git-context.ts, memory-index.ts
  compaction/ policy.ts, heuristic.ts, llm.ts, state-reminder.ts
  runtime/    agent-loop.ts, runtime.ts, observations.ts, dedup.ts,
              supervisors/{grounding,todo-gate}.ts, reminders/{background,subagents,todo-nudge,agents-md,skills}.ts
  observability/ audit.ts, metrics.ts, retention.ts
  cli/        main.ts, commands/*.ts, repl-fallback.ts, render/*.ts, json-output.ts, stop-reason.ts
  tui/        (M2) Ink app
  bridge/     (M4)      server/ (M4)      plugins/ (M4)
  diagnose/   env-checks.ts, agent-scenarios.ts, mock-scripts/*.ts
  eval/       runner.ts
tests/        mirrors src/; *.test.ts run by node --test; tests/golden/office/** (M0-captured)
docs/         ARCHITECTURE.md, PYTHON_IMPLEMENTATION_SUMMARY.md, THIRD_PARTY_NOTICES.md,
              WINDOWS_DEPLOYMENT.md, USING_WITH_COPILOT.md, legacy/ (the 2026-07/08 analysis docs)
.claw/skills/ unchanged
_legacy_python/   UNTRACKED (gitignored) — full copy of the Python tree for reference (§4)
```

One package, not a monorepo. The layering is enforced by lint, and a split
into `packages/*` can happen later if a second consumer appears.

### 3.3 Frozen external contracts (compatibility with what is already deployed)

- **Config keys**: every key in `_legacy_python/coding_agent/config/config.yml`
  and `docs/settings.workbench.yml` keeps its name, type and default:
  `provider.{name,type,base_url,api_key,model,chat_path,append_chat_path,api_version,verify_tls,ca_bundle,stream,streaming_mode,temperature,max_tokens,request_timeout_seconds,intelligence_tier,reasoning_effort,extra_headers,extra_body,omit_params}`,
  `runtime.*` (all 30 keys), `tools.{allowed,disabled}`, `mcp.servers[]`,
  `hooks.{pre_tool_use,post_tool_use,post_tool_use_failure,pre_compact,post_compact}`,
  `plugins.*`, `permission_rules`, `audit.enabled`, `instruction_files`,
  `logging.*`. New keys are additive (e.g. `provider.context_window`).
  Unknown keys warn, never fail.
- **Config discovery order** (`_legacy_python/coding_agent/config/settings.py:522`):
  `--config` or `~/.yucode/settings.yml` (seeded from bundled defaults if
  absent) → `~/.yucode/settings.json` → `<ws>/.yucode/settings.yml` →
  `.json` → `<ws>/.yucode/settings.local.yml` → `.json` → workspace
  `mcp.yml`. Deep-merge semantics: lists concatenate, dicts recurse, an
  empty overlay value never shadows a non-empty base value. Repo-local
  overlays pass the trust gate (`hooks`/`mcp` stripped unless trusted;
  `permission_mode` may only tighten).
- **Env overrides**: `YUCODE_API_KEY` (beats file), `YUCODE_PROVIDER_NAME`,
  `YUCODE_BASE_URL`, `YUCODE_MODEL`, `YUCODE_HOME`, `YUCODE_DANGEROUS_MODE`,
  `YUCODE_MCP_ROOT`, `YUCODE_AUTO_COMPACT_INPUT_TOKENS`,
  `YUCODE_STATE_RETENTION_MB/_DAYS`, `BRAVE_API_KEY`.
- **State directory**: `$YUCODE_HOME|~/.yucode/projects/<sha256(abs workspace path)[:12]>/`
  with the legacy `<ws>/.yucode` fallback; subdirs `sessions/ archives/
  checkpoints/ audit/ metrics/ tool_output/ bash_output/ exports/ plugins/`,
  files `todos.json`, `mcp.yml`. The hash input must be the same string
  Python used (`str(Path.resolve())`) — add a fixture test with a Windows
  path. Retention policy identical (`tool_output`/`bash_output` 32 MB/7 d,
  `archives` 32 MB/30 d, `checkpoints`/`metrics` 8 MB/30 d, never `audit/`
  or `sessions/`).
- **Memory store** (`~/.yucode/memory/*.md` + `MEMORY.md` index, workspace
  scope under `<ws>/.yucode/memory/`), frontmatter `name/description/saved_at/type`.
- **Skills & instruction files**: skill roots `<ws>/.yucode/skills`,
  `<ws>/.claw/skills`, `<ws>/.codex/skills`, `~/.yucode/skills`,
  `~/.claw/skills` (+ add `<ws>/.claude/skills` and `~/.claude/skills`, new);
  command roots `<ws>/.claude/commands`, `.yucode/commands`, `.claw/commands`;
  instruction filenames `YUCODE.md, YUCODE.local.md, .yucode/YUCODE.md,
  .yucode/instructions.md, AGENTS.md, AGENTS.local.md, .agents/AGENTS.md,
  CLAW.md, CLAW.local.md, .claw/CLAW.md, .claw/instructions.md, CLAUDE.md,
  CLAUDE.local.md`, cwd first then parents root-ward; caps 10k/file, 30k
  total; skill summary budget 4k with the 4 degradation tiers.
- **Tool names and JSON shapes** as listed in §A.2 — the two skills reference
  them by name. Error envelopes stay `{"error": "..."}`. MCP-namespaced
  tools stay `mcp__<server>__<tool>`.
- **Headless JSON output** of `yucode chat --json`: `final_text, stop_reason,
  session_id, iterations, usage{…,total_tokens}, usage_is_incomplete, events[]`;
  non-zero exit on `stop_reason ∈ {provider_error, stuck, max_tool_calls,
  max_iterations, empty_response, incomplete}`.
- **Event vocabulary** (wire format for `--json`, bridge, server, TUI):
  `provider_info, iteration_started, compaction, auto_compaction, reasoning,
  grounding_retry, todo_gate_retry, tool_call, tool_result, dedup_limit,
  stuck_exit, completed, error, assistant_delta, usage, warning, fallback`
  (+ new `turn_started`, `turn_ended`, `background_task`, `subagent`).
- **Hook protocol**: env `HOOK_EVENT/HOOK_TOOL_NAME/HOOK_TOOL_INPUT/HOOK_TOOL_IS_ERROR/HOOK_TOOL_OUTPUT`,
  JSON on stdin, exit 0 allow / 2 deny / other warn, 30 s timeout, matcher
  `name | a|b | /regex/`.
- **MCP**: stdio newline-delimited JSON-RPC, protocol `2024-11-05`+, 30 s
  per-call timeout with kill-on-timeout, stderr discarded, env = scrubbed
  environ + `config.env`.

### 3.4 Core interfaces (write these first; everything else is filled in behind them)

```ts
// src/tools/define.ts
type Permission = 'read-only' | 'workspace-write' | 'danger-full-access';
interface ToolDef<S extends z.ZodObject<any>> {
  name: string; description: string;          // description states the contract (fresh shell per call, spill path, …)
  parameters: S;                               // zod v4; JSON Schema via z.toJSONSchema()
  permission: Permission; risk: 'low'|'medium'|'high';
  readOnly?: boolean;                          // enables within-turn result cache + read-only streak advisory
  promptSection?: () => string;                // tool packs may contribute a system-prompt section (office routing)
  execute(args: z.infer<S>, ctx: ToolContext): Promise<string | object>;
  preview?(args: z.infer<S>, ctx: ToolContext): Promise<string>;   // for `prompt` mode / TUI confirmation
}
interface ToolContext {
  workspace: string; stateDir: string; config: AppConfig;
  resolvePath(p: string, opts?: { allowSpillRead?: boolean }): string;   // workspace boundary (§A.5)
  events: (e: AgentEvent) => void; signal: AbortSignal;
  background: BackgroundTaskTable; subagents: SubagentTable; mcp: McpManager;
  spill(name: string, text: string): string;   // returns path under state_dir/tool_output
}

// src/provider/types.ts
interface Provider {
  complete(req: { messages: ProviderMessage[]; tools: ToolSchema[]; signal: AbortSignal;
                  bodyOverrides?: Record<string, unknown>; onEvent?: (e: AgentEvent) => void }): Promise<AssistantResponse>;
}
interface AssistantResponse { text: string; toolCalls: ToolCall[]; usage: Usage; reasoning: string; finishReason: string; }

// src/session/events.ts  (append-only JSONL, one object per line)
type SessionEvent =
  | { seq; ts; type: 'session_created'; version: 2; sessionId; model; workspace }
  | { type: 'system_prompt'; text; hash }                      // recorded every time it changes
  | { type: 'user_message'; content; origin: 'user'|'supervisor:grounding'|'supervisor:todo_gate'|'file_ref' }
  | { type: 'assistant_message'; content; toolCalls; reasoning; finishReason; usage }
  | { type: 'tool_result'; toolCallId; name; content; isError; spilledPath? }
  | { type: 'compaction'; summary; droppedThroughSeq; archivePath; strategy }
  | { type: 'checkpoint'; label } | { type: 'note'; text };

// src/session/projection.ts
deriveMessages(events, opts: { systemPrompt; pruning?: RuntimeOptions }): ProviderMessage[]
//  – everything before the last `compaction` event collapses to its summary (system message)
//  – supervisor-origin user messages render as role "user" with the [SYSTEM SUPERVISOR] prefix (wire-compatible)
//  – request-time tool-result pruning tiers (keep_last_turns / soft trim / hard clear) exactly as
//    _legacy_python/coding_agent/core/session.py:36-72; never mutates the log

// src/runtime/agent-loop.ts
runTurn(input: { prompt; session: SessionLog; provider; tools: ToolRegistry; policy: PermissionPolicy;
                 hooks: HookRunner; options: RuntimeOptions; signal; depth }): AsyncGenerator<AgentEvent, TurnSummary>
```

Why the event log: the 2026-08 comparison (`docs/legacy/HARNESS_COMPARISON_2026-08.md`
§2) found five defects that all trace to "messages list = storage = model
input = audit record": unrecorded system prompt, destructive compaction with
an archive workaround, supervisor injections indistinguishable from user
text, dropped reasoning, write-only checkpoints. A projection over an
append-only log removes all five; in a greenfield it costs one ~150-line
module. Both TS references (dsh `session-persistence-jsonl`, pi's reducer
harness) do this.

### 3.5 Turn loop (port of `_legacy_python/coding_agent/core/runtime.py::_run_turn`, restructured)

Stage order is the spec — keep it exactly, but with the two extractions the
audit asked for (its C1): turn-stopping supervisors and post-tool reminders
are each an ordered list with a shared fire budget, called from one place.

1. `discoverProjectContext()` (instruction files, git status/diff ≤12k chars
   cut at a hunk boundary, skills summary, memory index) → `PromptAssembler.render()`;
   record `system_prompt` event if the hash changed.
2. Weak-tier investigation prefix (only when `resolvedTier()==='weak'`).
3. Append `user_message`. Resolve per-turn `reasoning_effort` (explicit wins; `auto` → `classifyReasoningEffort`).
4. Loop `for iteration in 1..max_iterations`:
   - emit `iteration_started`; `shouldCompact()` → compact (§3.8) → emit `compaction`.
   - `provider.complete(deriveMessages(...), tools.schemas(), {onEvent})`; record usage,
     `finishReason`, last `input_tokens`; emit `reasoning` if present; append `assistant_message`.
   - **No tool calls** → run **stop supervisors** in order: `grounding`
     (3 checks: `grep_matched_but_no_read` [weak+investigation],
     `claimed_success_but_bash_failed`, `time_sensitive_no_web_search`; ≤1
     fire/turn) then `todo_gate` (≤`todo_gate_max_fires_per_prompt`); a
     firing supervisor appends a `user_message{origin:'supervisor:*'}` and
     `continue`s. Otherwise `dedupRepetitiveResponse`, emit `completed`,
     persist metrics, `maybeAutoCompact`, return.
   - **Per tool call**: emit `tool_call`; canonical dedup key
     `(name, sha256(sortedJSON)[:16])`; ≥`dedup_tool_threshold` → synthetic
     `dedup_limit` error with escalating text, 3 blocks → `stuck_exit`;
     ≥`max_tool_calls` → budget error; read-only cache hit → reuse; else
     `executeTool()`; record observations; consecutive read-only advisory at
     8; run **post-tool reminders** (completed background bash, completed
     background subagents, todo nudge, AGENTS.md discovery, skill discovery)
     → appended as `<system-reminder>`; append `tool_result`; emit `tool_result`.
5. After the loop: stuck or exhaustion note, emit `completed`, metrics, `maybeAutoCompact`.

`executeTool()` pipeline (from `runtime.py:1809`): unknown-tool → permission
enforcer → policy (with prompter) → `PreToolUse` hook (deny stops) → parse
args (truncation-aware error) → zod validate → `execute` → merge pre-hook
feedback → `PostToolUse` / `PostToolUseFailure` hook → metrics + audit row
(session id, cwd, subagent id) → secret/PII redaction → 32 KB cap with
JSON-structure-preserving shrink and spill to `state_dir/tool_output`.

Depth tracking: pass `depth` explicitly through `runTurn` (no thread-locals).
Sub-agents run in-process as child `AgentRuntime`s (no threads needed).

### 3.6 Toolchain and dependency policy (the "no binaries" rule, mechanised)

- `npm ci --ignore-scripts` is the only install command, on every machine and in CI.
- `scripts/check-no-binaries.mjs`: walks `node_modules`, fails on any
  `*.exe|*.dll|*.node|*.so|*.dylib`, any `binding.gyp`, any package.json with
  `gypfile`, `prebuild-install`, `node-gyp-build`, or platform-specific
  `optionalDependencies`. Wired as `pretest` and a CI step. WASM is reported
  but allowed.
- `package.json` `overrides`: `"prebuild-install": "npm:nop@1.0.0"`,
  `"node-gyp-build": "npm:nop@1.0.0"` (Gemini CLI's trick).
- Build: `tsc -p .` → `dist/` (with `declaration: false`, `sourceMap: true`);
  run with `node --enable-source-maps dist/cli/main.js`. Dev/tests run the
  `.ts` sources directly under Node 24 type stripping → `erasableSyntaxOnly`
  in tsconfig, no `enum`, no `namespace`, no parameter properties, no
  `import x = require()`.
- Tests: `node --test "tests/**/*.test.ts"` with `node:test` + `node:assert/strict`;
  coverage via `--experimental-test-coverage`. No vitest.
- Lint/format: `eslint` (pure JS) with `typescript-eslint`, `prettier`. Not biome/oxlint (native).
- Allowed runtime deps for the harness (checked on npm 2026-09-12; re-run
  `check-no-binaries` after every `npm ci` anyway):
  `zod` 4.6.x (no deps) · `commander` · `yaml` · `undici` (no deps;
  `EnvHttpProxyAgent` forwards `connect:{ca,rejectUnauthorized}` to the
  underlying agents, reads upper- and lower-case `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`)
  · `@modelcontextprotocol/sdk` 1.30.x (MIT, Node ≥18; deps are express/hono/ajv/
  eventsource/jose/zod-to-json-schema — heavy but all pure JS, no optionalDependencies)
  · `ignore` · `picomatch` · `chalk`. M2 adds `ink` 7.x (MIT, Node ≥22, peer
  `react` ≥19.2; its `yoga-layout` 3.x is a WASM build, `node-pty` is only a
  devDependency there and is not installed for consumers). M3 adds the office
  libs from §5 M3. Anything else needs a note in the WP's commit message.
- M3 office deps (all verified pure JS): `fflate`, `@xmldom/xmldom`,
  `fontkit`, `pdfjs-dist` (pinned exactly; its `optionalDependencies` pull
  native `@napi-rs/canvas` packages, so **`.npmrc` must carry `omit=optional`**),
  `image-size`.
- Supply chain (from pi): `.npmrc` with `ignore-scripts=true`, `save-exact=true`,
  `omit=optional`; commit `package-lock.json`; `npm audit signatures` in CI.

### 3.7 Windows / locked-down-box hardening — Python mechanism → Node mechanism

Every row is a `yucode diagnose-env` check (M4) and, where marked, a unit test.

| Python mechanism (legacy path) | Node implementation |
|---|---|
| `core/shellexec.py` `"%COMSPEC%" /S /C "cmd"` with `shell=False` | `spawn(command, { shell: true })` — on Windows Node uses `process.env.ComSpec` and sets `windowsVerbatimArguments` automatically; Node's own wrapping is `/d /s /c "<command>"`. **Verify on the box** with the `shell_tool_cjk_subprocess` check (a command with >2 double quotes must round-trip). Test on Mac cannot prove this. |
| `tools/shell.py` `encoding="utf-8", errors="replace"`; child env `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` | Decode child stdout/stderr with `new TextDecoder('utf-8', { fatal: false })` (never `toString()` on a locale-dependent basis); tee raw bytes to `state_dir/bash_output/<id>.log`; keep injecting the two `PYTHON*` vars (harmless, and PowerShell/other children may still be Python elsewhere). |
| `cli.py` `sys.stdout.reconfigure(encoding="utf-8")` | Node writes UTF-8 to pipes and uses `WriteConsoleW` on a Windows TTY. Add `process.stdout.setDefaultEncoding('utf8')`; diagnose check `console_cjk_output`. |
| `security/env_scrub.py` (4 spawn sites) | Port `SECRET_ENV_PATTERNS` / `PRESERVED_ENV_PATTERNS` (proxy + CA + git author sets preserved) into `security/env-scrub.ts`; one `childEnv(context)` used by bash, hooks, plugins, MCP. Log names only, never values. |
| `verify_tls` / `ca_bundle` / proxy via urllib env | `undici`: `new Agent({ connect: { ca: [pem], rejectUnauthorized: verify_tls } })` passed as `dispatcher` to `fetch`; proxy via `undici.EnvHttpProxyAgent` (honours `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`) composed with the same connect options. `yucode.cmd` also sets `NODE_USE_ENV_PROXY=1` (Node 24 flag) as belt-and-braces. TLS failure message names `ca_bundle` first, `verify_tls: false` second. |
| read-only HOME fallback (`settings.py:539`) | Same: catch `EACCES/EPERM/EROFS` around seeding/reading `~/.yucode`, continue on bundled defaults with a warning naming `YUCODE_API_KEY`/`--config`. |
| `core/atomic_io.py` tmp + `os.replace` | `fs.writeFile(path + '.tmp')` then `fs.rename(tmp, path)` (libuv uses `MoveFileEx(REPLACE_EXISTING)` on Windows). Used by session log rotation, `todos.json`, `trusted_dirs.json`, config seeding. |
| no-ripgrep pure walk (`filesystem.py:752-847`) | `grep`/`glob`/`list` use an `fs.readdir({withFileTypes})` walk with in-place artifact-dir pruning (`.git node_modules __pycache__ .venv dist build .mypy_cache …`) and a 20k-file cap; use `rg` only if found on PATH, with a 60 s timeout falling back to the walk. Never depend on `@vscode/ripgrep`. |
| Ctrl+V via ctypes `CF_UNICODETEXT` | Windows Terminal pastes natively (bracketed paste). For legacy conhost in raw mode, `\x16` arrives as a keypress → fallback `spawn('powershell', ['-NoProfile','-Command','Get-Clipboard'])` (signed system binary; nothing new is created). CRLF→LF normalisation. |
| `yucode.cmd` `PYTHONPATH=%~dp0` + `python -m` | `yucode.cmd` = `@node "%~dp0dist\cli\main.js" %*`. Never `npm install -g`, never `npm link` (npm would generate `yucode.cmd`/`.ps1` shims only, no `.exe`, but there is no reason to). |
| long paths (`diagnose_env.py` `long_path_support`) | Node handles `\\?\`-free long paths only if the OS policy allows; keep the diagnose check, and prefix `\\?\` in `resolvePath` when a resolved path exceeds 240 chars on win32. |
| MCP 30 s timeout + kill (`plugins/mcp.py:246`) | Same: `AbortSignal.timeout(30_000)` around each request; on timeout `child.kill()` and reinit; stderr → `'ignore'`. |
| Retention sweep before handing a spill path to the model | Same, in `observability/retention.ts`, 300 s per-dir interval, 300 s in-use grace, live background logs protected. |

### 3.8 Compaction and context budget

Port `_legacy_python/coding_agent/memory/compact.py` + the runtime's LLM
compactor as-is (boundary walks back off `tool` messages; heuristic summary
with stats/tool names/recent requests/pending work/key files/24-line
timeline; LLM summary with the 7-section prompt, 2 attempts, <500-char
degenerate retry; transcript pointer to the archive; state re-injection of
edited paths / open todos / running tasks). Two deliberate changes:

- Add `provider.context_window` (default 0 = unknown). When set, the loop
  threshold becomes `0.85 × (context_window − min(20000, max_tokens||8192))`
  and request-time pruning kicks in at half of that; otherwise the existing
  absolute `compact_token_threshold` / half applies. This is IMPROVEMENT_PLAN
  §3.2 with the "acquire the number" blocker solved by a config field.
- Compaction is a `compaction` event + archive path; the log is never
  rewritten. `/clear` starts a new session id (IMPROVEMENT_PLAN A14).

Token estimate stays UTF-8 bytes / 3.2 (measured for CJK, §9 of the plan).
Do not touch the placement of the volatile prompt sections: moving them was
measured to have zero cache effect (IMPROVEMENT_PLAN §10).

---

## 4. Cutover procedure (M0, do in this order; steps 3–5 are destructive and need the owner's explicit go-ahead)

1. **Summarise the Python.** Write `docs/PYTHON_IMPLEMENTATION_SUMMARY.md`
   from §A of this plan plus direct reading: module map with LOC and
   GEN/DOM/DEAD tags, the tool catalogue with schemas, the turn-loop stage
   list, provider quirks, config keys, state layout, hardening list, test
   file → behaviour map. Cite `file:line` into the tree as it stands at the
   `python-final` tag. This is the document the porting agents read first.
2. **Close the two loose ends** the audit kept flagging: commit the deletion
   of `CLAUDE.md` (AGENTS.md is the instruction file), decide the local-only
   `v0.6.0` tag (recommend: delete it; the TS line starts at `1.0.0`).
3. **Tag and back up.** `git tag python-final` on that commit; push the tag.
   Copy the full tree to `_legacy_python/` (`git archive python-final | tar -x -C _legacy_python`)
   and add `_legacy_python/` to `.gitignore`. Also drop a zip outside the
   repo (`~/Desktop/yucode-python-final.zip`).
4. **Capture golden fixtures while Python still exists** — write and run
   the M3 capture script (§5 M3, WP-D0: `tools/golden/capture_office_goldens.py`,
   later kept under `_legacy_python/` and re-runnable from there) and commit
   `tests/golden/office/**` + `tests/golden/fonts/**`. Also capture the
   harness baselines the M1 WPs compare against: `show-config --output-format json`
   for the bundled and workbench configs, `definitions_for_provider()` JSON
   for every tool, `system-prompt` on a fixture workspace, a 3-turn
   `provider_messages()` fixture, `yucode eval --label python-final` JSON,
   and a real `diagnose_agent.py` digest. All under `tests/golden/harness/`.
   This must happen before step 5 and before the Mac loses Python.
5. **Reset the tree.** In one commit: `git rm -r` everything except `.git`,
   `.gitignore`, `.github/`, `LICENSE`, `.claw/`, `.claude/`, and move the
   analysis docs (`HARNESS_COMPARISON_2026-08.md`, `IMPROVEMENT_PLAN_2026-08.md`,
   `AGENT_UPGRADE_NOTES.md`, `WORKBENCH_PLAN.md`, `CHANGELOG.md` → `CHANGELOG-python.md`,
   the Chinese 實戰指南, `docs/USING_WITH_COPILOT.md`, `docs/settings.workbench.yml`)
   under `docs/legacy/`. Keep `tests/eval_prompts.yaml` (moves to
   `tests/eval/prompts.yaml`) and `tests/golden/`. History and tags stay
   (owner's decision: no orphan branch).
6. **Scaffold** (WP0 below) and commit "Begin the TypeScript rewrite".

---

## 5. Milestones and work packages

Sizes: S < 300 LOC, M 300–800, L 800–1500, XL > 1500 (TS incl. tests).
"Read" = files under `_legacy_python/coding_agent/` unless stated. "Copy" =
reference-repo material worth reading before writing. "Tests" = the Python
test files whose cases are the checklist (port the ones that encode a real
failure; the commit message or comment usually says which). "Done when" = the
acceptance command.

### M0 — Preparation (owner + one agent)

**WP0 — Summary, cutover, scaffold, policy.** (M)
Deliverables: steps 1–6 of §4; `package.json`, `tsconfig.json`, `.npmrc`,
`eslint.config.js`, `scripts/check-no-binaries.mjs`, `.github/workflows/ci.yml`
(Node 24 on ubuntu + **windows-latest**, `npm ci --ignore-scripts`,
`check-no-binaries`, `tsc --noEmit`, `node --test`), `AGENTS.md` (TS
conventions: §7 of this plan condensed), `docs/ARCHITECTURE.md` (§3),
`docs/THIRD_PARTY_NOTICES.md` (empty table), `yucode.cmd`, `yucode`.
Done when: `npm ci --ignore-scripts && npm test` passes with one smoke test,
`check-no-binaries` is green, CI green on both OSes.

**WP-D0 — Golden fixture capture (Python side, before the reset).** See M3.

### M1 — Headless core (usable as `yucode chat "…" --json` on the Mac against DeepSeek)

**WP1 — Config.** (M) Read `config/settings.py`, `config/trust.py`,
`config/simple_yaml.py` (only for merge/coercion semantics; use the `yaml`
package), `config/config.yml`. Deliver `src/config/*`: zod schema mirroring
every dataclass field with defaults, `loadAppConfig()` with the §3.3
discovery/merge/env rules, `stateDir()`, trust store, `resolveIntelligenceTier()`
(`_WEAK_MODEL_HINTS`), `toControlDict()`/`asPromptSafeDict()` (api_key masked).
Tests: `test_init_config.py` (merge, empty-overlay, env precedence, trust
gate, permission clamp), `test_restricted_env_hardening.py` (read-only HOME).
Done when: loading `docs/legacy/settings.workbench.yml` and the bundled
default yields the same control dict the Python `show-config --output-format json` printed (capture one in WP0).

**WP2 — Provider.** (L) Read `core/providers.py` (all 1,403 lines), `core/errors.py`,
`core/session.py::AssistantResponse/Usage`. Copy: Gemini CLI
`contentGenerator.ts` decorator idea. Deliver `src/provider/*`: single
`OpenAICompatibleProvider` over `fetch` + undici dispatcher (§3.7 TLS/proxy);
URL builder (`chat_path` absolute or appended; `api_version` → Azure
deployment URL + `api-key` header); body builder (`omit_params`, `extra_body`,
`max_tokens` 0 = omit, learned ceiling clamp, `reasoning_effort`); SSE parser
with the two stall timers (120 s transport, 300 s content) and
`stream_options.include_usage`; `streaming_mode` `hybrid|no_stream|stream`;
retry ladder (5 attempts, 1.5 s base ×2, ±20 % jitter, `Retry-After`
honoured ≤60 s, retryable `{429,500,502,503,504,520-524,529}`, backoff waits
on the abort signal); in-stream retry budget 2 while nothing was emitted;
per-host circuit breaker (3 failures, 30 s cooldown, half-open; 429/503 count
only on the final attempt); 400 self-heal (≤4 rounds: drop param, rename
`max_tokens↔max_completion_tokens`, learn ceiling, pin `reasoning_effort:none`
on the tools+reasoning refusal; learned corrections persist per instance);
text-tag tool-call fallback (`<tool_call>`, `<function_call>`, control-char
JSON repair) and the streaming filter that hides `<think>`/`<reasoning>`
blocks; usage normalisation (OpenAI vs Anthropic vocab, cache buckets folded
so `total = input + output`); `reasoning_content` extraction; `finish_reason`
kept; typed errors (`ProviderError`, `RetriesExhaustedError`,
`ContextWindowExceededError`, non-recoverable TLS). Decorators:
`RecordingProvider` (per-call wall time, tokens), `MockProvider` (scripted
responses; tool calls in the script are executed for real by the loop),
`LoggingProvider`.
Tests: `test_provider_parsing.py` (100), `test_provider_retry.py` (50),
`test_deepseek_adaptation.py` (29) — port with recorded SSE fixtures
(`tests/fixtures/provider/*.sse`). Done when: `yucode doctor` against the
real DeepSeek endpoint on the Mac passes stream + no-stream + tools probes.

**WP3 — Session log + projection.** (M) Read `core/session.py`,
`memory/compact.py::estimate_*`, `core/runtime.py::_archive_before_compact/checkpoint`.
Copy: dsh `session-persistence-jsonl` shape. Deliver `src/session/*` per §3.4:
append (atomic line append with `fs.appendFile` + `fsync` on turn end),
read/replay, `deriveMessages` with pruning tiers, `latest` pointer file,
`listSessions()` (title = first user message ≤80 chars), archive on
compaction, bytes/3.2 estimator, `_safe_tool_args` replacement of malformed
tool-call JSON on replay. Tests: the session/pruning cases in `test_core.py`,
`test_grokbuild_improvements.py` (pruning tiers, transcript pointer),
`test_audit_and_spill.py` (session id correlation). Done when: a scripted
3-turn session replays to the identical provider message list the Python
`provider_messages()` produced (fixture captured in WP0).

**WP4 — Tool framework + generic tools.** (XL; split into 4a framework, 4b fs+patch, 4c shell+web, 4d misc+memory)
Read `tools/__init__.py` (registry, aliases, name index, `_resolve_path` +
spill exemption, background/subagent tables, `_registry_handler` envelope),
`tools/filesystem.py`, `tools/patch.py`, `tools/shell.py`, `tools/web.py`,
`tools/misc.py`, `tools/memory_tools.py`, `memory/store.py`, `tools/notebook.py`,
`core/runtime.py::_truncate_tool_output/_shrink_json_strings`. Copy: Gemini
CLI `tools/tools.ts` (two-phase declaration/invocation, confirmation
details), Gemini `tools/grep.ts` (JS grep), opencode's one-`.txt`-per-tool
descriptions as the style for contract-stating descriptions
(IMPROVEMENT_PLAN C3: `bash` must state fresh shell per call, cmd.exe on
Windows, safety refusals are policy not retryable, output already spilled).
Deliver the 29 generic tools (§A.2) with identical names/params/JSON shapes:
`read_file` (offset/limit, binary-hint map `.xlsx→inspect_excel_sheets` etc.,
did-you-mean on ENOENT), `read_files` (≤10), `write_file`, `edit_file`
(no-op rejection, ambiguity rejection, nearest-match and Unicode-confusable
hints, post-write syntax check for `.json/.yaml/.ts/.js/.py`-by-regex where
cheap), `apply_patch` (Codex envelope, exact match, all-or-nothing,
`splitlines` + trailing-newline preservation), `glob_search` (fuzzy
fallback), `grep_search` (pure walk + optional `rg`, workspace-bounded,
relative paths), `list_directory`, `file_outline` (regex outline for
`.py/.ts/.js`; keep the name), `bash` (spawn with shell, tee + spill, 32 KB
JSON-safe cap, `working_directory`, timeout, `run_in_background`,
`auto_background_on_timeout`, background table + completion reminders, env
scrub, safety verdict), `web_fetch` (size/redirect caps, HTML→text,
`<untrusted-content>` wrapping on disk and in preview, overflow spill to
`tool_output/web_fetch/`), `web_search` (Brave key → DuckDuckGo HTML → relaxed
retry), `todo_write` (merge, atomic, corrupt-tolerant), `AskUserQuestion`
(non-TTY guard, 30-min timeout), `load_skill`, `config_read`,
`structured_output`, `mcp_list_resources`/`mcp_read_resource`, the five
`memory_*` tools + store, `edit_notebook_cell`. Drop `sleep`, `config_write`,
`tool_search`, every extended stub.
Tests: `test_shell_tool.py` (23), fs/patch/web cases in
`test_grokbuild_improvements.py`, `test_grokbuild_round3.py`, `test_improvements.py`,
`test_v060_features.py`, `test_memory_system.py` (42), `test_audit_and_spill.py`
(spill exemption is read-only and scoped). Done when: every tool's JSON
Schema (via `z.toJSONSchema`) matches the Python `definitions_for_provider()`
output captured in WP0 (property names, required, types), and the tool tests pass on macOS **and** the windows-latest CI runner.

**WP5 — Security.** (M) Read `security/permissions.py` (585), `security/safety.py`,
`security/env_scrub.py`. Do **not** port `bash_validation.py` (dead) or
`sandbox.py` (bwrap, Linux only). Deliver `permissions.ts` (5 modes +
`PERMISSION_ORDER` + aliases; rule matching with `*`-only wildcard; subject
extraction from `command|path|file_path|uri|name`; shell candidate expansion
on `&& || ; | & $( ` ( ) { }` and newlines, `KEY=val` and wrapper stripping
(`sudo/env/timeout/nice/xargs/eval/exec/…` with value-taking flags), `sh -c`
unwrap ≤3 levels; deny → hook override → ask → allow(whole command only) →
prompt → level compare with the remediation message naming the settings
key; `PermissionEnforcer` non-interactive; read-only command list incl.
cmd.exe builtins and `_MUTATING_INDICATORS`), `safety.ts` (5 regex families
incl. Windows drive-root forms; verdicts), `redact.ts` (secret + PII scan on
every tool output), `env-scrub.ts` (§3.7). Record the known prefix-matcher
ceiling (`/bin/rm -rf /`, split flags, `$(...)`, here-strings, `xargs -I{}`)
in a test file as `todo` tests so nobody re-discovers it.
Tests: `test_security.py` (66). Done when: all 66 ported cases pass.

**WP6 — Hooks.** (S) Read `hooks/__init__.py`. Deliver `hooks/runner.ts`
with the §3.3 protocol, matcher, 30 s timeout, scrubbed env, Windows spawn
via `shell: true`. Add optional `session_start`, `user_prompt_submit`,
`stop` events (Claude Code / Gemini vocabulary) behind the same config
shape; keep the five legacy keys. Tests: hook cases from `test_core.py`,
`test_grokbuild_round3.py` (matcher). Done when: an exit-2 hook blocks a tool
and the denial text reaches the model as the tool result.

**WP7 — Prompt assembly, instruction files, skills.** (M) Read
`memory/prompting.py`, `memory/skills.py`, `runtime.py:485-491` (dynamic
discovery lists — unify them with the static ones), `tools/misc.py::load_skill`.
Deliver `src/prompt/*` with the same section order (intro, system incl. the
`<untrusted-content>` rule, doing-tasks incl. workspace-search protocol and
web-research workflow, [tool-pack sections], web-freshness with today's date,
memory rules, executing-actions, weak-tier section when weak, environment,
project context with the capped diff, memory index ≤8k, instruction files
≤10k/30k, skills summary ≤4k with 4 tiers, masked config dump when
`config_dump_in_prompt`). Frontmatter parser: first-colon `key: value`,
quotes stripped — same limits as Python. Tests: prompt cases in
`test_grokbuild_improvements.py`, `test_v060_features.py`, `test_improvements.py`
(weak-tier section gating). Done when: `yucode system-prompt` on a fixture
workspace equals the Python output captured in WP0 modulo the date line and
the "Runtime config" JSON key order.

**WP8 — Compaction.** (M) Read `memory/compact.py`, `runtime.py::compact/_maybe_auto_compact/_make_llm_compactor/_build_compaction_state_reminder`,
`core/response_dedup.py`. Deliver §3.8. Tests: compaction cases in
`test_grokbuild_improvements.py` (transcript pointer, structured summary,
degenerate retry, state reminder), `test_core.py`, `test_v060_features.py`
(response dedup). Done when: a 40-message fixture compacts to the same
preserved tail and the archive contains the system prompt.

**WP9 — Runtime loop, supervisors, reminders, observability.** (L) Read
`core/runtime.py` end to end, `observability/metrics.py` (audit, metrics,
retention), `core/coordinator.py::looks_like_investigation/is_complex_prompt/classify_reasoning_effort`
(only these helpers are needed in v1). Copy: cline `sdk/ARCHITECTURE.md`
(stateless/stateful split), dsh agent-loop README (cancellation semantics:
preserve already-streamed text). Deliver `src/runtime/*` per §3.5 and
`src/observability/*` (audit JSONL rows with session id/cwd/subagent id;
metrics per turn/session with a caller; retention sweep). Tests:
`test_grokbuild_improvements.py` (reminders, dedup escalation, stuck exit,
read-only cache, todo nudge/gate), `test_improvements.py` (grounding checks,
observations), `test_audit_and_spill.py` (audit identity, spill), `test_core.py`
(turn summary, budgets), `test_restricted_env_hardening.py` (breaker under a
dead gateway aborts mid-ladder). Done when: `yucode chat "list the files here and summarise" --json`
against DeepSeek returns a grounded answer with `stop_reason: completed` and
exit 0, and a forced provider failure exits non-zero with `provider_error`.

**WP10 — Headless CLI.** (L) Read `interface/cli.py` (subcommands, flags,
`_FAILURE_STOP_REASONS`, `_stop_reason` paragraph anchoring, `init-config`
wizard, `doctor` probes), `interface/commands.py` (`@path` inlining caps
8 KB/24 KB), `interface/render.py` (event renderer for non-TTY: `→ tool…` /
`✔ result` lines; Windows VT enable is unnecessary in Node ≥ 16 but keep the
ASCII-spinner fallback when `!isatty`). Copy: codex `docs/exec.md` for the
headless contract. Deliver `src/cli/*` with commander: `chat` (prompt |
stdin | interactive fallback), `--workspace --model --permission-mode
--allowed-tools --resume --json/--output-format`, `doctor`, `init-config
[--defaults]`, `show-config`, `config-path`, `trust [--remove|--list]`,
`skills`, `status`, `system-prompt`, `version`, `mcp-list/add/remove/validate/preset`,
`init`, `init-agents`, `init-memory` (port `interface/init_workspace.py`,
`memory_bootstrap.py`), plus a minimal line-based REPL (`repl-fallback.ts`)
used when the TUI is unavailable. Tests: `test_interface_verdicts.py` (48),
`test_event_handler.py` (40), `test_init_config.py` wizard cases. Done when:
the JSON payload schema and exit codes match §3.3 on the same scenarios the
Python tests use.

**WP11 — MCP client (+ transitional office preset).** (M) Read
`plugins/mcp.py`, `plugins/mcp_servers/_protocol.py` (only for the path-guard
semantics, which move into the TS tool layer in M3), `settings.py:462-486`
(`mcp.yml`), `cli.py:2041` presets. Use `@modelcontextprotocol/sdk`
`Client` + `StdioClientTransport` (verify pure JS; also accept
`StreamableHTTPClientTransport` since it is free). Deliver `src/mcp/{client,manager,presets}.ts`:
namespacing, discovery failure → server disabled with phase, 30 s timeout +
kill + one reconnect, env scrub + `config.env`, `mcp_list_resources`/`mcp_read_resource`
backing. Presets: `office` → during M1–M2 points at
`python -m coding_agent.plugins.mcp_servers.office_mcp <root>` inside
`_legacy_python` (Mac only, transitional); replaced in M3 by native tools.
Tests: `test_mcp_wire_format.py` (13) — the TS client against the SDK's
server, and against the legacy Python server while Python is on the Mac.
Done when: `yucode mcp-validate` lists 22 tools from the legacy office server.

**WP12 — Mock provider + behavioural diagnostic (the parity gate).** (L)
Read `diagnose_agent.py` (scenarios, judges, mock scripts, `_loop_analysis`,
digest format), `interface/eval.py`, `tests/eval_prompts.yaml`. Deliver
`yucode diagnose-agent [--mock] [--only id]` with the 8 scenarios
(`code_reading, surgical_edit, bash_execution, databook_stages,
honesty_missing_file, error_recovery, needle_haystack, multi_step_pipeline`)
building throwaway temp workspaces, judges, trace JSON, digest; `databook_stages`
needs the office tools, so until M3 it runs through the transitional MCP
preset. Deliver `yucode eval --suite --out --only --label` with the three
mechanical criteria and non-zero exit on regression. Tests: the "mock mode
all scenarios pass" pin from `test_restricted_env_hardening.py`. Done when:
`--mock` gives 24/24 (25/25 once M3 lands) and a real DeepSeek run on the
Mac matches the Python's 25/25 with "no redundant calls, no re-reads".

**M1 exit gate:** WP12 green in mock and real modes; `npm test`, `tsc
--noEmit`, `check-no-binaries` green on macOS and windows-latest CI.

### M2 — Interactive

**WP13 — Sub-agent tool + background tables.** (M) Read `tools/agent_tool.py`,
`runtime.py::_check_completed_subagent_tasks`, `coordinator.py::ROLE_TOOLS`
(for the role → tool-set idea only). Deliver `tools/agent/subagent.ts`: roles
`research|work|validate` whose tool sets are **derived from tool permission
class** (read-only / write / write+bash) instead of hard-coded names — this
removes the domain-name pollution the inventory found at `coordinator.py:35-77`;
depth cap (default 1, configurable), `run_in_background`, 300 s foreground
join then convert-to-tracked, `[INCOMPLETE: …]` stop-reason banner, results
via post-tool reminder, audit `subagent_id`. Tests: `test_grokbuild_round3.py`
(background subagent), A1/A2 cases in `test_audit_and_spill.py`. Done when:
a background sub-agent's result arrives as a `<system-reminder>` on the next
tool result.

**WP14 — Ink TUI.** (XL; split 14a input/text buffer, 14b transcript +
streaming + tool cards, 14c slash commands + `@file` + history + permission
prompt) Read `interface/cli.py` REPL section (key bindings, paste handling
lessons, Ctrl+C semantics: clear buffer when text present else interrupt,
Enter submits, Alt+Enter newline, completion popup for `/` and `@`),
`interface/commands.py` (28 slash commands), `interface/render.py`
(`StreamingTextDisplay` erase-on-finalise, `ProgressDisplay` tool cards,
markdown→ANSI, cost summary, startup banner with loaded instruction files
and git branch). Copy: Gemini CLI `packages/cli/src/ui/components/shared/text-buffer.ts`
and its input hook (multi-line, paste = chunk containing `\n`), Gemini's
history/completion components; keep Apache headers + notices. Deliver
`src/tui/*` on upstream `ink` + `react` consuming the AgentEvent stream;
`~/.yucode/history` file; slash commands (`help status config tools mcp
skills clear exit quit compact diff memory resume permissions branch commit
plugins agents save metrics cost pr log worktree stash export model version
remember forget init`); `prompt`-mode confirmation UI using `ToolDef.preview`;
Windows Ctrl+V fallback (§3.7). Tests: text-buffer unit tests (port the
*behaviours* of `test_interactive_input.py`: standalone Enter submits, a
3-line paste with embedded newlines submits as one block, popup-accept takes
precedence) with `ink-testing-library` if pure JS, else drive the buffer
model directly. Done when: the paste test passes on the owner's Windows box
in both Windows Terminal and legacy conhost (this is the check the Python
version never got confirmed on).

**M2 exit gate:** daily use on the Mac for a week without falling back to
Python; the owner confirms paste on the Windows box.

### M3 — Domain layer port (22 office tools + notebook, Python as the oracle)

Designed from a line-by-line read of `tools/office.py`, `text_metrics.py`,
`databook_metrics.py`, `pdf_tables.py` and their tests, with every library
verified on the npm registry (2026-09-12). Four findings shape the design:

1. **Pillow measures text with FreeType's hinted, pixel-rounded advances**,
   not raw glyph widths (`getlength("Cash increased.")` at 9 pt Arial = 68.0;
   the unhinted sum is 65.03). Verified glyph-by-glyph with zero mismatches:
   `ppem = round(size)`, `x_scale = ((ppem*64)<<16 + upem/2) / upem`,
   `adv26 = (advUnits*x_scale + 0x8000) >> 16`, `px = ((adv26+32) & ~63) / 64`;
   no kerning; missing glyphs use `.notdef`; `getmetrics()` =
   `(ceil(MulFix(hhea.ascender)), −floor(MulFix(hhea.descender)))`. The
   calibrated constants (`text_metrics.py:40-42`) were tuned against this,
   so `fonts.ts` must emulate that integer pipeline on top of fontkit's
   font-unit advances. Bonus: text-metric goldens can be **exact**.
2. **`pdfjs-dist` pulls native binaries by default** (`optionalDependencies:
   {"@napi-rs/canvas"}` → 11 platform packages with `.node` files). Text
   extraction works without it. Hence `.npmrc` gets `omit=optional` and the
   binary gate must run after every install.
3. **pdf.js `getTextContent()` cannot reproduce pdfplumber words** (one item
   per show-text chunk, no per-glyph positions). The faithful port runs
   `getOperatorList()` through a ~200-line port of pdfminer's text-state
   machine, then ports `WordExtractor` and `to_textmap`. This is the one XL
   package and the schedule risk.
4. **`financial-statement-snip` step 6 shells out to `python scripts/build_workbook.py`**
   (`.claw/skills/financial-statement-snip/SKILL.md:66-69`). Not one of the
   22 tools; breaks when Python leaves the box. Owner decided: port it as a
   TS tool (WP-D13) and change that one line of the skill.

Two openpyxl behaviours the Python inherits are bugs, and the owner decided
to **fix** them in the port (goldens carry the corrected expectation, marked
in `manifest.json`): `_get_visibility_state` (`office.py:537-553`) hides
only the **first** column of a hidden range (`<col min="3" max="5" hidden="1"/>`
→ `{3}`; TS hides 3..5); and `write_excel_cell` round-trips through openpyxl,
which drops charts/pivots (TS edits the sheet XML in place). A third quirk is
mirrored, not fixed: openpyxl's builtin number-format table lacks ids
27–36/50–58 so CJK-locale dates come back as raw serials, which is exactly
why `_parse_date_like` recovers serials (`databook_metrics.py:102-106`).

**Libraries (verdict, evidence):** use **fflate** 0.8.x (zip, no deps),
**@xmldom/xmldom** 0.9.x (namespace DOM + serializer, no deps), **fontkit**
2.0.x (pure JS incl. `brotli` JS port; `.ttc` collections → `fonts[0]`
mirrors Pillow `index=0` for `msyh.ttc`), **pdfjs-dist** 6.x pinned exactly
(Apache-2.0, Node ≥22.13; `standardFontDataUrl` → its `standard_fonts/`,
`useSystemFonts:false`, `disableFontFace:true`, `isEvalSupported:false`),
**image-size** 2.x (no deps). Rejected: `exceljs` (rewrites workbooks on
save, 40 transitive pkgs), npm `xlsx` (frozen at 0.18.5, real releases only
on SheetJS's CDN), `read-excel-file` (no hidden API), `docx`/`mammoth` (no
style-name parity), `pptxgenjs` (generation-only, own 16:9 template),
`pdf-parse` (hard dep on `@napi-rs/canvas`), `opentype.js` (no TTC).
Vendored assets (MIT): python-pptx `default.pptx` (11 layouts, 4:3),
`notesMaster.xml`, `notes.xml`, `theme.xml`; python-docx `default.docx`;
add rows to `THIRD_PARTY_NOTICES.md`. Test fonts (OFL):
`LiberationSans-Regular.ttf` (Arial-metric-compatible) and a `pyftsubset`
subset of `NotoSansSC-Regular` renamed per OFL rules, committed under
`tests/golden/fonts/`.

**Module layout `src/tools/office/`:**

```
index.ts            registers the 24 tools (22 office + notebook + build_workbook; zod schemas + execute);
                    office prompt section (routing table)
workbook_build.ts   build_workbook: spec JSON → house-format .xlsx (styles.xml builder, Index tab, formulas, checks)
py.ts               pyRound (half-even, exact-tie detection), pyStr (bool/float/datetime/time/timedelta repr),
                    pyFloatParses (Python float() grammar), cpSlice (code-point slicing), pyListRepr
ooxml.ts            openPackage/savePackage (fflate, entry order kept), parseXml/serializeXml, NS constants,
                    rels + [Content_Types] helpers, insertInSchemaOrder
excel.ts            workbook model: sheets {name,state,part}, sharedStrings (rich text concatenated), styles →
                    isDate/isTimedelta per cellXfs (openpyxl STRIP_RE/TIMEDELTA_RE + BUILTIN_FORMATS 0-49),
                    cells from every <c>, min/max row/col, hidden rows/cols, iterRows(), fromExcel(serial)
excel_write.ts      writeCell: ordered <row>/<c> insert, inlineStr | <f> | error; preserves charts/pivots
databook_metrics.ts verbatim port (label tuple order preserved)
word.ts             body paragraphs (run text mapping, style UI names via BabelFish table), append, new doc
pptx.ts             presentation model: slides in sldIdLst order, layouts, shapes (type string, dims with
                    layout→master placeholder inheritance, text, tables), findShapeByName
pptx_text.ts        clearTextFrame, setParagraphs (9 pt, lnSpc 100 %, spcBef 0, spcAft 3 pt, a:latin only),
                    setAutofit (noAutofit | bounded normAutofit)
pptx_build.ts       addSlide(layoutIdx) (new part + rels + content type, sldId ≥ 256, placeholder cloning rules),
                    notes slide (+ vendored notesMaster/theme when absent), rebuildTable(graphicFrame)
fonts.ts            platform font tables (text_metrics.py:44-80 incl. fc-match fallback), loadFont (fontkit),
                    FreeTypeMetrics {ppem, xScale, advancePx, ascentPx, descentPx}, cache by (family,size,cjk)
text_metrics.ts     isPredominantlyChinese (>30 %), atomize, wrapParagraph, applyKinsoku, textBoxFromShape,
                    estimateCapacity
pdf_chars.ts        pdf.js loader; operator-list text-state machine → chars {text,x0,x1,top,bottom,size,upright}
pdf_text.ts         WordExtractor port (x/y tol 3, ligature expansion), clusterObjects, toTextmap (layout), plainText
pdf_tables.ts       extractPageWords, extractPeriodTable (verbatim port)
image.ts  notebook.ts  assets/ (default.pptx, notesMaster.xml, notes.xml, theme.xml, default.docx)
```

**Behaviours that are easy to lose (per tool; the implementing agent keeps this list open):**

- `read_excel_sheet`: iterates the openpyxl **bounding box**, so blank rows inside it appear as `[""…]`; header = first row with any `<c>` element (style-only cells count); `data_only` semantics (formula → cached `<v>`, none → `""`); break test `len(rows) >= max_rows+1` *before* append; `skipped` counts hidden rows met before the break; `note` only when `skipped or hidden_cols`; `str()` parity for `True`, `100.0`, `1e-07`, `2019-12-31 00:00:00`, `10:30:00`, `1 day, 2:00:00`.
- `list_excel_sheets`: `hidden` and `veryHidden` both count; `hidden_sheets_count` key only when > 0. `inspect_excel_sheets`: append-then-break at `sample_rows+1`; `estimated_data_rows = max(0, max_row-1-len(hidden_rows))`; key is `hidden_sheets`. `excel_to_json`: `""` header → `col_{i}`, returns a JSON array. `read_excel_preview`: `_is_numeric` is Python `float(s.replace(",",""))` (accepts `1_000`, `inf`, `+5`, `1.`, `-.5`; rejects `0x10`); 70 % rule over first 20 truthy rows; `preview = data[:10]`.
- `write_excel_cell`: leading `=` → formula, error literals → `t="e"`, else string; edit the sheet XML in place (deliberate improvement: keeps charts/pivots openpyxl drops); return `Wrote '{value}' to {cell} in {name}`.
- `locate_databook_stage_columns`: scans raw values **including hidden rows**; `scan_end = min(min_row+max_scan_rows, max_row)`; label order `示意性调整数/數` before `示意性调整`; `len(text) > 35` rejects; ≥ 2 matches required; densest row wins, first on ties; date lookback 3 rows; serial window `32874..54970`, integer-valued, non-bool; unit marker = first hit scanning rows `[:basis_row+1]`; `stage_columns` in ascending `col_idx`.
- `read_word_text`: body-level `w:p` only (tables excluded); `w:tab→\t`, `w:br`/`w:cr→\n`, `w:noBreakHyphen→-`; truncation by code points. `read_word_paragraphs`: style is the UI name (`heading 1`→`Heading 1`), default paragraph style when no `pStyle`. `write_word`/`append_word`: unknown style → `no style with name 'X'`; default style writes no `pStyle`; insert before `w:sectPr`.
- `read_pptx`: only `p:sp` shapes have text; `pic/chart/tbl/clipArt` placeholders don't; `a:br → \v`; strip per shape, skip empty. `inspect_pptx_shapes`: type strings `PLACEHOLDER (14)`, `FREEFORM (5)`, `AUTO_SHAPE (1)`, `TEXT_BOX (17)`, `TABLE (19)`, `CHART (3)`, `PICTURE (13)`, `GROUP (6)`, `LINE (9)`, else `None`; **placeholders without `a:xfrm` inherit position from the layout placeholder with the same `idx`, then the master by type** (house templates rely on this); `round(emu/914400, 2)` is half-even; table preview = first 3 rows; `text_preview` = 150 code points; top-level `spTree` only.
- `fill_pptx_shape_text`: `tf.clear()` keeps paragraph 1's `pPr`/`endParaRPr`; spacing elements in schema order; runs `sz="900"` + `a:latin typeface="Arial|Microsoft YaHei"` only; `\n`/`\v` → `a:br`; autofit: remove all three, then `noAutofit` if `capacity is None or fits` (**no font found ⇒ noAutofit, not an error**), else `normAutofit fontScale=round(scale·1e5) lnSpcReduction=round(max(0,(1−scale)·0.5)·1e5)` with scale clamped `[0.70, 1.0]`; error strings must contain `use fill_pptx_table instead`, `out of range`, `Available shapes: [...]`.
- `fill_pptx_table`: **delete + rebuild, never fill in place**; new `p:graphicFrame` appended at the **end** of `spTree`; id = `max(//@id)+1`; name and (possibly inherited) `xfrm` restored; `tblPr firstRow="1" bandRow="1"` + `tableStyleId {5C22544A-7EE6-4342-B048-85BDC9FD1C3A}`; `gridCol w = width//cols` with the last absorbing the remainder, rows likewise; cell text splits on `\n` into paragraphs; `style_id` only replaces the existing `a:tableStyleId` text; empty rows/cols → error; works on a `p:sp` table placeholder too.
- `estimate_pptx_text_capacity`: missing font → `ValueError` (the skill relies on it to fall back); box = shape size minus the slide shape's own `bodyPr` insets, defaults 91440/91440/45720/45720 EMU; CJK ratio > 30 % over non-space chars; `_atomize` (CJK char = atom, Latin word = atom, separator only between two Latin atoms); greedy wrap with per-char fallback; kinsoku pulls a leading char from the set at `text_metrics.py:88` back to the previous line, deleting emptied lines; `line_h = ascent+descent` (integers); `para_height = lines*line_h + 3`; `fill_ratio` 2 dp; `remaining_lines_estimate = max(0, floor(remaining/line_h))`.
- `write_pptx`: string slide → layout 1 title only; `layout` outside `[0,len)` → 1; title = placeholder **idx 0**; body = placeholder with `idx == 1` gated by `len(placeholders) > 1`; notes only if truthy; return `Created {path} with N slides`. `write_pptx_from_template`: only `layout_idx >= len` falls back (negative wraps like Python); return text says `from template`.
- `image_read`: suffix allow-list; `mimetypes` mapping (`.jpg→image/jpeg`); relative `path` only when strictly inside the workspace; `mode` derived for PNG/JPEG/GIF/BMP else omitted; 1,500,000-byte cap → `base64_skipped` with thousands separators.
- `read_pdf_text`: `layout=false` → words → lines clustered by `top` (tol 3) joined by single spaces; `layout=true` → pdfplumber `to_textmap` (`x_density 7.25`, `y_density 13`, half-even rounds, padded lines, terminal newline stripped). `extract_pdf_words`: pdfminer `LTChar` model (`adv = width/1000·fs·(Tz/100)`, bbox through `Tm×CTM`, `top = page_h − y1`, charspace/wordspace rules, TJ kerns, `/Rotate` folded into the CTM); word split on whitespace, `cx < ax`, `cx > bx+3`, `|Δtop| > 3`; ligatures expanded; 2 dp half-even. `extract_pdf_period_table`: rows by `|top−first.top| ≤ 2.5`; header = topmost row with all periods or ≥ 2 year tokens; bands: single centre ±60 else midpoints with outer margin `max(min(gap)/2, 20)`; **first parsed value per band wins**; dash = 0 **only inside a band**; bracket negatives; label-only rows merge first; attach within 14 pt, **tie → next**; orphans → `skipped_label_rows`; `band_x_ranges` 1 dp.
- `edit_notebook_cell`: index may exceed length by ≤ 100 (appends code cells, `execution_count: null`); every source line ends with `\n`; writes `indent=2` + trailing newline.
- Path boundary: every path arg through `ctx.resolvePath` (§A.5); on Windows compare case-insensitively, tolerate `\\?\` from `realpath.native`, emulate `resolve(strict=False)` by canonicalising the deepest existing ancestor.

**WP-D0 — Golden capture (Python, runs in M0 before the reset; re-runnable).** `tools/golden/capture_office_goldens.py` builds deterministic synthetic fixtures (`databook.xlsx` with hidden row / single hidden col / hidden **range** / real datetime / raw serial `43830` / CJK custom date format / builtin numFmt 31 / formula with cached `<v>` / bool / `#N/A` / `inlineStr` / rich-text shared string / `0.30000000000000004`, `1e-7`, `1e16`, a `Cash` sheet starting at C3, an empty sheet, `hidden` + `veryHidden` sheets; `template.pptx` with the `test_office_tools.py:79-94` shapes plus custom-inset textbox, autoshape, group, connector, picture, a stale `normAutofit fontScale="62500"`, an `a:br` paragraph, a layout-1 slide with inheriting placeholders + notes, the `tinyBox` overflow case, a "Two Content" slide, a `p:sp` table placeholder; `doc.docx` with Normal/Heading 1/2/Title/List Bullet, tab + line break, a table paragraph to exclude, an emoji; `note.pdf` from `tests/test_pdf_tables.py::_make_pdf` plus a page with `TJ` kerns, `Tc`/`Tw`, `Tz 90`, `Td`/`T*`, a blank page, a single-period page; `rot.pdf` with `/Rotate 90`; `img.png` RGBA 3×2, `img.jpg`, `img.gif`; `nb.ipynb`), runs every tool × normal/edge/error args (~70 cases) through `ToolRegistry.execute`, and writes `tests/golden/office/{fixtures,cases.json,manifest.json}`. Text-metric cases run twice: `portable` (font resolver monkeypatched to the committed OFL fonts) and `system` (keyed by resolved font path + SHA-256); also records `glyph_advances_px` per distinct char and `metrics_px`. Write tools record **projections** computed with lxml from the output package (bodyPr + autofit + paragraph spacing/runs; graphicFrame name/id/spTree index/xfrm/gridCols/rowHeights/tableStyleId/cells; slide parts/layout/shapes/notes; `[{styleId, text}]`; the written `<c>`). `manifest.json` records Python/lib versions, `raqm=false`, FreeType version, font hashes, and the pdf.js version to pin.

**TS comparison policy (`tests/golden/office.test.ts`):** deep-equal after JSON parse for all Excel tools, Word reads, `read_pptx`, `inspect_pptx_shapes`, notebook, `image_read` (except `mode`), period-table values/labels/periods/skipped, and error substrings; **exact** for `estimate_pptx_text_capacity` and `fill_pptx_shape_text` with the portable fonts; system-font cases run only when the machine's font hash matches the manifest, else `fits` must agree unless `|fill_ratio−1| < 0.1`, `line_count` ±1, `used_height_pt` ±(line_h+3); `extract_pdf_words` text/order/count exact, `x0/x1/x_center` ±0.01, `top/bottom` ±0.5; `top`/`band_x_ranges` ±0.5/±0.05; `read_pdf_text` exact (documented normaliser fallback if a `round(y_dist)` boundary flips); write-tool projections deep-equal. Independent unit tests: `py.ts` (`pyRound(4.125,2)==4.12`, `pyRound(0.5)==0`, `pyStr(100.0)=="100.0"`, `pyStr(1e-7)=="1e-07"`), `fonts.ts` vs `glyph_advances_px`, kinsoku, `databook_metrics` and `pdf_tables` cases ported verbatim.

**Work packages (dependency order; hand off one at a time):**

| WP | Content | Depends | Size |
|---|---|---|---|
| D0 | Golden capture script + fixtures + fonts + manifest (Python, in M0) | — | M |
| D1 | `.npmrc omit=optional`, pinned deps, `py.ts` + tests, `ooxml.ts`, office `resolvePath` semantics | WP0 | M |
| D2 | `excel.ts` + 6 read tools + `databook_metrics.ts` | D1 | L |
| D3 | `excel_write.ts` / `write_excel_cell` | D2 | S |
| D4 | `pptx.ts` read model, `inspect_pptx_shapes`, `read_pptx` (placeholder inheritance) | D1 | L |
| D5 | `fonts.ts` + `text_metrics.ts` + `estimate_pptx_text_capacity` | D4 | M |
| D6 | `pptx_text.ts` `fill_pptx_shape_text`; `fill_pptx_table` | D4, D5 | L |
| D7 | `word.ts` + 4 tools + vendored `default.docx` | D1 | M |
| D8 | `image.ts`, `notebook.ts` | D1 | S |
| D9 | `pdf_chars.ts` + `pdf_text.ts` + `read_pdf_text` (plain + layout) | D1 | XL |
| D10 | `pdf_tables.ts`: `extract_pdf_words`, `extract_pdf_period_table` | D9 | M |
| D11 | `pptx_build.ts`: `write_pptx`, `write_pptx_from_template`, notes, vendored template | D4 | L |
| D12 | `yucode mcp-serve office <root>` (SDK `McpServer` + stdio; same root-guard; " (writes to disk)" suffix); retire the transitional Python preset; port the 72 domain tests' intent | all | S |
| D13 | **`build_workbook` tool** (new, 23rd office tool): reads a spec JSON per `.claw/skills/financial-statement-snip/references/spec-schema.md` and writes the house-format workbook per `references/house-format.md` (Index tab with title block + hyperlinked rows; one tab per table: navy title band row 1, light-blue sub-header row 2 with the units label in B2, data from row 3, panes frozen at B3, column A width 3.4, labels in B, optional Notes column, `sum`/`sum_range`/`values` → live `=SUM()`/`=a+b-c` formulas, `subtotal`/`total`/`header`/`blank` row styling, "Checks (should be nil)" block with `= built − printed` formulas, number format `_(* #,##0_);_(* (#,##0);_(* "-"??_);_(@_)`, Arial 8 body, title font with Arial Bold fallback, row heights 22.8/19.5/12.0, gridlines hidden, fit-to-one-page-wide portrait; sheet names ≤31 chars, deduped). Raw OOXML via `ooxml.ts` + a hand-written `styles.xml` builder (fonts, fills, borders, numFmts, cellXfs). Then edit SKILL.md step 6 to call the tool (`build_workbook(spec_path, output_path)`) instead of `python scripts/build_workbook.py`, and delete `scripts/README.md`. **No Python oracle exists on this Mac** (`find` for `build_workbook.py` returns nothing; the skill's README says to copy it from the original Claude skill package). If the owner retrieves it before the reset, D0 captures goldens for it; otherwise tests are structural assertions on the produced XML derived from the two reference docs plus one hand-checked example spec, and the workbook is opened in Excel once by the owner. | D3 | L |

Order for a single agent: D0 → D1 → D2 → D3 → D4 → D5 → D6 → D7 → D8 → D9 → D10 → D11 → D13 → D12. D9 is the schedule risk; a `getTextContent()`-based fallback is acceptable **only** for `read_pdf_text layout=false`, never for the coordinate tools.

**M3 exit gate:** golden suite green on macOS and windows-latest; `diagnose-agent --mock` 25/25 with native `databook_stages`; both skills run end to end on the synthetic fixtures.

**Deliberate deviations (decided):** hide the whole `min..max` hidden-column range (fix); `write_excel_cell` preserves charts/pivots (fix); JSON output not `ensure_ascii`-escaped (harmless); `a:ea` typeface not added (mirror Python); next-unused slide part name instead of `len(sldIdLst)+1` (harmless fix). Each is a named case in the golden manifest.

### M4 — Peripherals kept in v1

**WP20 — `diagnose-env`.** (M) Port the 15 checks from `diagnose_env.py`
(§A.6) as `yucode diagnose-env [--network] [--workspace]`; output
paste-friendly, stderr of failing probes on their own indented lines, no
assistant branding in the closing lines (standing owner preference).
Done when: 15/15 on the Mac; the owner runs it on the box and pastes the digest.

**WP21 — VS Code JSONL bridge.** (S) Read `interface/bridge.py` (handshake /
load_config / chat messages, LRU of 8 live runtimes, evict-saves-and-notices).
Deliver `src/bridge/*` speaking the same JSONL protocol over stdio, one
runtime per workspace, same eviction. Done when: the existing protocol tests
(`test_event_handler.py` bridge cases) pass.

**WP22 — HTTP + SSE server.** (M) Read `interface/server.py` (endpoints
`POST/GET /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/message`,
`GET /sessions/{id}/events`). Deliver on `node:http` (no express): bind
`127.0.0.1` only, a per-run bearer token printed once at startup and
required on every request, `Origin` check, no CORS wildcard — the two
opencode CVEs are the reason. Done when: an unauthenticated request is 401
and the SSE stream carries the §3.3 event vocabulary.

**WP23 — Plugins.** (M) Read `plugins/__init__.py` (`plugin.json` manifest:
tools as executables, hook commands, `init`/`shutdown` lifecycle with 60 s
timeout, install via `git clone` with 300 s timeout, enable/disable). Deliver
`src/plugins/*`; plugin tools run through the same `childEnv` scrub and the
same tool pipeline. Trust gate applies (repo-local plugin dirs need `yucode trust`).
Done when: a fixture plugin contributes one tool and one `PreToolUse` hook.

**WP24 — Docs and deployment.** (M) `README.md` (rewrite; keep the
"Windows note" idea: never `npm install -g`, never `npm link`, `yucode.cmd`
only), `docs/WINDOWS_DEPLOYMENT.md`, `docs/USING_WITH_COPILOT.md` (now
`node dist/cli/main.js mcp-serve office <root>`), `CHANGELOG.md` starting at
`1.0.0`, `docs/THIRD_PARTY_NOTICES.md` complete. Deployment artefact
(owner's preference: local/offline first): `scripts/bundle.mjs` runs
`npm ci --ignore-scripts --omit=optional`, `npm run build`,
`check-no-binaries`, then zips `dist/ node_modules/ config/ .claw/ yucode.cmd
package.json` into `yucode-<version>-node.zip`; the doc's primary path is
"unzip, add the folder to the user PATH, run `yucode diagnose-env`", with
`npm ci` on the box as the alternative. Because every dependency is pure
JS/WASM the same `node_modules` works on Windows and macOS; the bundle
script asserts that by grepping the lockfile for `os`/`cpu` fields.

### M5 — Cutover

- Windows box: `node --version` ≥ 24, unzip `yucode-<version>-node.zip`
  (or `npm ci --ignore-scripts`), `npm run check:binaries` on the box too,
  `yucode diagnose-env` 15/15, `yucode doctor`,
  `yucode diagnose-agent` real run, REPL paste test, `.vscode/mcp.json`
  pointed at `mcp-serve`, both skills exercised end to end on synthetic
  inputs.
- Tag `v1.0.0`. Remove the transitional MCP preset. Keep `_legacy_python/`
  locally until the owner deletes it; it is never committed.

---

## 6. Verification (end-to-end, run before calling any milestone done)

1. `npm run check:binaries && npm run lint && tsc --noEmit && npm test` — green on macOS and on the windows-latest CI runner.
2. `yucode diagnose-agent --mock` — every scenario's judge passes (24/24 before M3, 25/25 after).
3. `yucode diagnose-agent` against DeepSeek on the Mac — same verdicts as the Python baseline (25/25, no redundant calls, no re-reads, 2–5 iterations per task).
4. `yucode eval --suite tests/eval/prompts.yaml --label ts-m1` vs the Python `--label python-final` JSON captured in WP0 — no verdict regressions; compare `max_tool_calls` per prompt.
5. Golden office tests (M3) — byte-identical JSON where the design says so, tolerances where it says tolerance.
6. Skills: `fdd-commentary` on a synthetic databook + template produces a filled PPTX whose `bodyPr` matches the golden; `financial-statement-snip` on the synthetic bilingual PDF reproduces the golden period table (Kas/Cash `[1410, 25641]` never swapped).
7. Windows box checklist in M5.

---

## 7. Rules for implementing agents (put a condensed copy in `AGENTS.md`)

1. **Port behaviour, not structure.** Read `docs/PYTHON_IMPLEMENTATION_SUMMARY.md`
   and the WP's listed legacy files before writing. Where the Python was
   written against an observed failure (the comment or commit says so),
   keep that behaviour and its test. Where it was ported from claw-code, do
   not port it.
2. **Evidence legend** from `IMPROVEMENT_PLAN_2026-08.md` applies to any
   deviation from the Python behaviour: [M] measured / [V] verified in code /
   [R] read in a reference repo / [A] assumed. Don't ship an [A].
3. **No dead code.** Nothing from §A.7 gets ported. No speculative
   configurability. If a WP would add an abstraction with one caller, don't.
4. **Pure JS only.** Run `npm run check:binaries` before every commit. New
   runtime dependencies need a line in the commit message saying why and a
   note that its dependency tree was checked.
5. **Frozen contracts** (§3.3) are tests, not prose: add a fixture test when
   you touch one.
6. **Erasable TypeScript only** (`erasableSyntaxOnly`): no `enum`, no
   `namespace`, no parameter properties. Use `as const` objects and union
   types.
7. **Windows first.** Use `node:path` everywhere, normalise backslashes in
   user input, never assume symlinks, never assume `/bin/sh`. Every WP's
   tests must pass on the windows-latest runner.
8. **Licences.** Copied files keep their header; add a row to
   `docs/THIRD_PARTY_NOTICES.md`. Never copy from leaked Claude Code source,
   from `@anthropic-ai/claude-agent-sdk`, or from any proprietary package.
9. **Secrets and names.** Placeholders only (`api.workbench.example.internal`,
   `x-tenant-*`, `FILL-ME`). Check the diff for hostnames, keys, client or
   firm names before committing — the repo was scrubbed once already.
10. **Commits.** One WP → one or a few commits, message explains the why in
    the existing house style (see `git log` before `python-final`). No
    `Co-Authored-By`, no "Generated with" footers. Do not push unless asked.
11. **Owner's working style** (`~/.claude/CLAUDE.md`): think before coding,
    simplicity first, surgical changes, verifiable stopping condition per
    task, verify don't estimate. Communicate with the owner in Traditional
    Chinese; code, comments, commit messages in English.
12. **Destructive git** (`rm -r`, orphan branches, force push, tag deletion)
    needs the owner's explicit yes each time, even in auto-accept sessions.

---

## 8. Explicitly not in v1 (with the reason, so it is not re-litigated)

- **Coordinator** (`core/coordinator.py`, 950 LOC). Owner's decision. v2
  item: port with role tool-sets by permission class, a validator that gets
  tools (`_validate` passes `tools=[]` today), stall detection (A8), and the
  A/B harness (A15) run first at n ≥ 10.
- `bash_validation.py`, `sandbox.py` (bwrap), the four registries,
  `task_packet.py`, `summary_compression.py`, `observability/logging.py`,
  the 33 extended tool stubs, `finance_mcp` (yfinance; needs network the box
  refuses), `sleep`, `config_write`, `tool_search` — dead or pointless.
- A provider registry / Vercel AI SDK / `openai` npm client. One gateway
  class; the quirks are the value (WORKBENCH_PLAN §5, IMPROVEMENT_PLAN §5).
- Effect-TS, Cordis, or any plugin-bus runtime.
- `node:sqlite` session store — still experimental in 24; JSONL is enough
  and grep-able.
- Windows ACL/Job Object sandboxing (IMPROVEMENT_PLAN C5: only if a runaway
  process actually happens).
- Real LSP client. Post-edit syntax check covers the valuable part.
- Importing Python-era `sessions/*.json`.

Deferred to v2 alongside the coordinator: `tool-pwsh` (dsh), Context Epoch,
shadow-git snapshots, `SessionStart/Stop` hook semantics beyond the
optional keys in WP6, HTTP MCP presets, `--experimental-test-coverage` gates.

---

## 9. Open questions for the owner (answer before the step they gate)

1. Is WASM in `node_modules` (Ink → yoga-layout) acceptable to the box's EDR? If not, M2 falls back to the readline REPL and Ink waits. (Gates WP14; check on the box during M0 by copying one `.wasm` file over.)
2. Delete the local-only `v0.6.0` tag? (Gates §4 step 2; recommend delete.)
3. Should `file_outline` survive as a regex outline, or be dropped? (Gates WP4; recommend keep, it is small.)
4. Can the original `build_workbook.py` + `assets/example_spec.json` be retrieved from the Claude skill package before the reset? (Gates D0/D13: with it, goldens; without it, docs-derived tests.)

---

## A. Appendix — Python implementation summary (seed for `docs/PYTHON_IMPLEMENTATION_SUMMARY.md`)

Paths are relative to `_legacy_python/coding_agent/` (= tag `python-final`).

### A.1 Module map (LOC; GEN = generic harness, DOM = domain, DEAD = unreachable by default)

core/: runtime.py 1980 GEN · providers.py 1403 GEN · coordinator.py 950 GEN (v2) ·
session.py 399 GEN · errors.py 144 GEN · response_dedup.py 120 GEN · shellexec.py 35 GEN ·
atomic_io.py 21 GEN · task_registry.py 176 DEAD · team_cron_registry.py 175 DEAD ·
worker_registry.py 264 DEAD · task_packet.py 65 DEAD · summary_compression.py 106 DEAD
tools/: __init__.py 784 GEN · filesystem.py 1051 GEN · patch.py 362 GEN · shell.py 336 GEN ·
web.py 503 GEN · agent_tool.py 239 GEN · memory_tools.py 201 GEN · misc.py 155 GEN ·
notebook.py 38 GEN · office.py 1245 DOM · text_metrics.py 328 DOM · pdf_tables.py 261 DOM ·
databook_metrics.py 170 DOM
interface/: cli.py 2418 · render.py 1092 · eval.py 404 · commands.py 298 · bridge.py 285 ·
init_workspace.py 278 · server.py 230 · memory_bootstrap.py 98 (all GEN)
memory/: prompting.py 520 (GEN; `_office_files_section` at :410 is DOM prose) · compact.py 485 ·
store.py 245 · skills.py 198
security/: permissions.py 585 · safety.py 216 · env_scrub.py 187 · sandbox.py 224 (skip) ·
bash_validation.py 165 (DEAD)
config/: settings.py 1072 · simple_yaml.py 202 · trust.py 153 · config.yml
hooks/__init__.py 278 · plugins/__init__.py 366 · plugins/mcp.py 448 ·
plugins/mcp_servers/{_protocol.py 201, office_mcp.py 84, finance_mcp.py 176}
observability/: metrics.py 484 · logging.py 66 (no caller)
Repo root: diagnose_env.py, diagnose_agent.py, test_connection.py, yucode.cmd, yucode.bat.
`build/lib/` is a stale setuptools artifact — ignore.

### A.2 Tool catalogue (name → required params · permission)

Generic (29): read_file(path; offset, limit)·RO — write_file(path, content)·WW —
edit_file(path, old_string, new_string; replace_all)·WW — glob_search(pattern; target_directory)·RO —
grep_search(pattern; path, glob)·RO — list_directory(; path)·RO — read_files(paths; offset, limit)·RO —
file_outline(path)·RO — apply_patch(patch)·WW — bash(command; timeout, working_directory,
run_in_background, description)·DFA — web_fetch(url; prompt)·RO — web_search(query;
allowed_domains, blocked_domains)·RO — edit_notebook_cell(path, cell_index, new_source)·WW —
agent(prompt; role, allowed_tools, run_in_background)·WW — todo_write(todos; merge)·WW —
mcp_list_resources(server)·RO — mcp_read_resource(server, uri)·RO — load_skill(name)·RO —
config_read()·RO — structured_output(data)·RO — memory_save(name, description, type, content; scope)·WW —
memory_list(; scope)·RO — memory_read(name; scope)·RO — memory_delete(name; scope)·WW —
memory_search(query)·RO — AskUserQuestion(question; options)·RO. Dropped: sleep, config_write, tool_search.
Aliases: read/write/edit/glob/grep/ls. Name lookup is case- and underscore-insensitive.

Domain (22, all in office.py): read_excel_sheet, list_excel_sheets, inspect_excel_sheets,
write_excel_cell, excel_to_json, read_word_text, read_word_paragraphs, write_word, append_word,
read_pptx, write_pptx, write_pptx_from_template, inspect_pptx_shapes, fill_pptx_shape_text,
fill_pptx_table, estimate_pptx_text_capacity, locate_databook_stage_columns, read_excel_preview,
image_read, read_pdf_text, extract_pdf_words, extract_pdf_period_table.
New in TS (M3 WP-D13): build_workbook(spec_path, output_path)·WW.

### A.3 RuntimeOptions defaults (dataclass / bundled config.yml where different)

permission_mode workspace-write · max_iterations 25/32 · max_worker_steps 20 ·
max_coordinator_retries 3 · orchestration_mode auto · max_tool_calls 80 · dedup_tool_threshold 3
(weak tier → 2) · auto_save_session true · auto_resume_latest true · compact_preserve_recent 4 ·
compact_token_threshold 60000/10000 (10k measured at 23 % cache hit vs 36 % at 120k — raise) ·
compact_strategy heuristic · error_strategy resilient · shell_timeout_seconds 30 ·
shell_env_filter_secrets true · include_git_context true · config_dump_in_prompt true ·
prune_tool_results true · prune_keep_last_turns 3 · prune_soft_trim_threshold 4000 ·
prune_soft_trim_head/tail 1500 · prune_hard_clear_turns 10 · auto_background_on_timeout false ·
todo_nudge_enabled true (3 turns since write, 5 min gap) · todo_gate_enabled false (max 2) ·
agents_md_discovery_enabled true · skill_discovery_enabled true ·
YUCODE_AUTO_COMPACT_INPUT_TOKENS 100000.

### A.4 Provider constants worth carrying verbatim

`_MAX_RETRIES 5`, backoff base 1.5 s ×2 ±20 %, `Retry-After` clamp 60 s, retryable
{429,500,502,503,504,520,521,522,523,524,529}, `_MAX_STREAM_RETRIES 2`, transport stall 120 s,
content stall 300 s, breaker threshold 3 / cooldown 30 s / half-open, `_ORIGIN_DOWN_STATUS_CODES`
{521-524}, `_MAX_SELF_HEAL_ROUNDS 4`, `_RE_MAX_TOKENS_CEILING`, `_TRUNCATED_FINISH_REASONS`,
MCP call timeout 30 s, hook timeout 30 s, plugin lifecycle 60 s, git clone 300 s,
`_MAX_TOOL_RESULT_BYTES 32768`, `_MAX_CONSECUTIVE_READONLY 8`, `_MAX_STUCK_DEDUP_BLOCKS 3`,
`_BYTES_PER_TOKEN 3.2`, `MAX_GIT_DIFF_CHARS 12000`, `MIN_SUMMARY_SEED_CHARS 500`.

### A.5 Path boundary (`tools/__init__.py:672-720`)

`_resolve_path`: normalise backslashes → resolve canonically (symlink-safe) → must be inside the
workspace; a narrow **read-only** exemption for `state_dir/tool_output` and `state_dir/bash_output`
(never `audit/ sessions/ archives/ checkpoints/ metrics/`), opt-in per call (`allow_spill_read`).
`grep_search`'s `path` must go through it (was the one hole; fixed 2026-08-26). The MCP server's
`_guard_paths` rewrites path-like args (`path|paths|file|filename|directory|dir|*_path|*_file|*_dir`)
to the resolved absolute path before the handler runs — keep that behaviour inside the TS office
tools and `mcp-serve`.

### A.6 `diagnose_env.py` check names

home_state_dir_writable · atomic_write_on_state_dir · session_save_load_roundtrip ·
console_cjk_output (WARN) · long_path_support · config_load · git_resilience · tls_context ·
circuit_breaker_unit · pure_python_file_walk (→ pure_js_file_walk) · shell_tool_cjk_subprocess ·
mcp_timeout · prompt_toolkit_windows_input (→ tui_windows_input) · repl_ctrl_v_paste_binding ·
gateway_reachability (`--network`; any HTTP status = PASS; report whether HTTPS_PROXY was set).

### A.7 Do-not-port list

task_registry, worker_registry, team_cron_registry, task_packet, summary_compression,
observability/logging, bash_validation, sandbox (bwrap), the 33 extended tool stubs
(`tools/__init__.py:380-413`), `sleep`, `config_write`, `tool_search`, finance_mcp,
`tests/test_parity_harness.py`, `interface/eval.py`'s never-read `acceptable_conclusions`
auto-matching (keep them as human-review echoes, as today).

### A.8 Test files → behaviour (port checklist)

test_provider_parsing 100 · test_provider_retry 50 · test_deepseek_adaptation 29 → WP2 ·
test_security 66 → WP5 · test_init_config 39 → WP1/WP10 · test_shell_tool 23 → WP4 ·
test_grokbuild_improvements 95 · test_grokbuild_round3 43 · test_improvements 41 · test_core 46 ·
test_audit_and_spill 59 · test_restricted_env_hardening 29 · test_v060_features 47 ·
test_memory_system 42 → WP3/4/7/8/9 · test_interface_verdicts 48 · test_event_handler 40 → WP10/21 ·
test_mcp_wire_format 13 → WP11 · test_coordinator 20 → v2 · test_interactive_input 33 → WP14
(behaviours only) · test_office_tools 24 · test_text_metrics 15 · test_databook_metrics 20 ·
test_pdf_tables 13 → M3 · test_parity_harness 46 → drop.
