# Changelog

All notable changes to this project will be documented in this file.

## [0.6.0] - 2026-05-17

Focus: give the agent durable memory across sessions, stop it from fumbling Office/PDF files, anchor it to today's date so it stops answering time-sensitive questions from stale training data, and add a runtime grounding check that forces web verification on time-sensitive prompts (failing-loud rather than silently hallucinating). Plus a workspace `/init` scanner, an image-inspection tool, a Brave-then-DDG search fallback chain, and a user-profile memory bootstrap.

### Added — persistent memory subsystem
- `coding_agent/memory/store.py`: NEW `MemoryStore` — file-backed persistent memory with two scopes: user (`~/.yucode/memory/`) and workspace (`<workspace>/.yucode/memory/`). Each memory is a standalone `.md` file with YAML-style frontmatter (`name`, `description`, `saved_at`, `type`); `MEMORY.md` per scope is a rebuilt one-line-per-entry index. Four memory types: `user`, `feedback`, `project`, `reference`. Files are plain markdown — user can edit them with any text editor.
- `coding_agent/tools/memory_tools.py`: NEW five tools — `memory_save`, `memory_list`, `memory_read`, `memory_delete`, `memory_search`. All five are registered as built-ins in `tools/__init__.py::_builtin_tools`. `memory_list` returns metadata only (cheap); `memory_read` returns the full body (call once names are known).
- `coding_agent/memory/prompting.py::_memory_section`: NEW prompt section. When MEMORY.md is non-empty in either scope, the index is auto-injected into the system prompt with instructions to call `memory_read` for full bodies. Truncates at 8K chars so a large user-scope memory store can't crowd out instruction files.
- `coding_agent/memory/prompting.py::_memory_rules_section`: NEW prompt section with explicit "when to save" guidance (covers the four memory types, what to save vs. NOT save, and the dedupe rule "call `memory_list` before saving").
- `MemoryEntry.saved_at` + `MemoryEntry.age_days()`: every memory now records the ISO save date; `MemoryStore.load_indexes_text()` annotates entries older than `STALE_DAYS_THRESHOLD` (30 days) with `[stale: Xd]` in the prompt-injected index. Mirrors Claude Code's stale-memory marker so the model can deprioritise old facts.
- `coding_agent/interface/cli.py`: NEW `/remember [-w] [--type=TYPE] <text>` slash command — save a persistent memory in one line without invoking the tool layer. Auto-derives the slug from the first 5 words and the description from the first sentence. Default scope = user, type = user.
- `coding_agent/interface/cli.py`: NEW `/forget <name>` slash command — delete a persistent memory by name.
- `coding_agent/interface/render.py::format_memory_display`: new `persistent_memories` parameter. `/memory` now shows a `Persistent Memory (cross-session)` section listing every saved memory with its scope/type tag.
- `coding_agent/interface/memory_bootstrap.py`: NEW `bootstrap_user_profile()` + `yucode init-memory` CLI subcommand. Scans `~/.gitconfig` for name/email, `$SHELL`, `$EDITOR`, `platform`, and a small allow-list of tools on PATH (rg/fd/fzf/gh/docker/poetry/pnpm/...) and writes a `user-profile` memory in the user scope. Idempotent; pass `--force` to refresh.

### Added — office, image, and binary-file routing
- `coding_agent/memory/prompting.py::_office_files_section`: NEW prompt section mapping file extensions to dedicated office tools (`.xlsx` → `inspect_excel_sheets` + `read_excel_sheet`, `.docx` → `read_word_text`, `.pptx` → `read_pptx`, `.pdf` → `read_pdf_text`, `.ipynb` → `edit_notebook_cell`, `.png` / `.jpg` → `image_read`). Explicitly steers the agent away from `cat`/`xxd`/`file`/`hexdump` on Office files — those were defaulting it into failure loops on Windows.
- `coding_agent/tools/filesystem.py::_BINARY_TOOL_HINTS`: extension → tool-suggestion map. `_read_file` checks the suffix BEFORE the generic binary-bytes check and raises a tool-specific error (e.g. opening `report.xlsx` with `read_file` now says "use `inspect_excel_sheets` then `read_excel_sheet`" instead of "use bash xxd/file/hexdump"). Covers .xlsx/.xlsm/.xls/.docx/.pptx/.pdf/.ipynb/.png/.jpg/.jpeg/.webp/.gif/.bmp/.tiff.
- `coding_agent/tools/office.py::_image_read`: NEW `image_read(path, include_base64?)` tool — returns mime_type, size_bytes, width/height/mode (via Pillow if available, else a degradation note). Pass `include_base64=true` to also get a data-URL base64 payload (capped at 1.5 MB raw) for multimodal providers.

### Added — web grounding
- `coding_agent/memory/prompting.py::_web_freshness_section`: NEW prompt section anchored to `current_date`. Tells the agent its training data is older than today and that any claim about current state (companies, products, prices, schedules, policies, exchange rates, news) MUST be verified via `web_search` + `web_fetch` before answering. Calls out the failure mode of naming dissolved companies / discontinued brands from memory.
- `coding_agent/core/runtime.py::_TIME_SENSITIVE_RE` + `_is_time_sensitive_prompt()`: regex detector covering English (latest/current/now/today/this year/as of/stock price/...) + Chinese (最新/現在/今天/今年/匯率/股價/...). Used at turn start to flag the prompt as time-sensitive.
- `coding_agent/core/runtime.py::_check_final_answer_grounding`: NEW third check `time_sensitive_no_web_search` — fires when the prompt is time-sensitive AND the model produced a final answer without ever calling `web_search` or `web_fetch`. Injects a supervisor message and forces one retry iteration. Emits `grounding_retry` event with `reason=time_sensitive_no_web_search`. Applies to ALL tiers (not just weak) because even frontier models trip on dissolved-company / retired-product questions.
- `coding_agent/core/runtime.py::_record_tool_observation`: now records `web_searched` / `web_fetched` invocations so the new grounding check can short-circuit.
- `coding_agent/tools/web.py`: `web_search` rewritten as a backend fallback chain. Order: (1) Brave Search API when `BRAVE_API_KEY` env var is set; (2) DuckDuckGo HTML scraping; (3) DuckDuckGo retry with a relaxed query (strips quotes + ≤2-char filler words) on zero-hit. Response shape changed from `list[{title,url}]` to `{results: list, _meta: {backends_tried: list, hint?: str}}`; render.py updated to display the new shape.

### Added — workspace bootstrap
- `coding_agent/interface/init_workspace.py`: NEW `detect_profile()` + `write_agents_md()` + `render_agents_md()`. Scans for `pyproject.toml`/`requirements.txt`/`setup.py`, `package.json` + `tsconfig.json` + lockfiles, `Cargo.toml`, `go.mod`, `Makefile`; detects framework hints (fastapi/flask/django/react/vue/next/express/rails/spring); extracts test/build/run commands from package.json scripts; reads README tagline; reads git remote.
- `yucode init-agents` CLI subcommand + `/init [--force]` slash command — generates a starter `AGENTS.md` with the detected stack, common commands, notable files, and empty conventions/notes sections for the user to fill in. Refuses to overwrite an existing file without `--force`.

### Added — AGENTS.md recognition
- `coding_agent/memory/prompting.py::discover_instruction_files`: now recognises `AGENTS.md`, `AGENTS.local.md`, and `.agents/AGENTS.md` alongside the existing `YUCODE.md` / `CLAW.md` / `CLAUDE.md` candidates — opencode parity.

### Added — coordinator subagent tooling
- `coding_agent/core/coordinator.py::ROLE_TOOLS`: research workers gain `memory_list` / `memory_read` / `memory_search` + the office-inspection tools (`inspect_excel_sheets`, `read_excel_sheet`, `read_excel_preview`, `read_word_text`, `read_pptx`, `read_pdf_text`) so a research subagent can consult prior context AND read Office documents directly. Work workers gain `memory_list` / `memory_read` (read-only — workers should not silently create memories). Both gain `read_files` for batch reads.

### Added — tests
- `tests/test_memory_system.py`: NEW 40 tests covering MemoryStore CRUD, index rebuild, scope precedence, slugify edge cases, the five memory tools, prompt-integration (memory index loaded, sections present, AGENTS.md recognised, index truncation), `_read_file` binary-extension routing, `/remember` and `/forget` CLI handlers, coordinator ROLE_TOOLS memory entries.
- `tests/test_v060_features.py`: NEW 49 tests covering `_is_time_sensitive_prompt` (English + Chinese positive/negative cases), the `time_sensitive_no_web_search` grounding check matrix, `MemoryEntry.age_days` + stale-marker annotation in `load_indexes_text`, `image_read` (metadata, base64 toggle, size cap, extension routing), `detect_profile` for Python/Node/Rust/Go, `render_agents_md`, `write_agents_md` (overwrite protection + force), `bootstrap_user_profile`, and the `_web_search` fallback chain (Brave → DDG → relaxed retry, mocked).

### Changed
- `coding_agent/memory/prompting.py::ProjectContext`: gained a `memory_index: str = ""` field. `discover_project_context` populates it by calling `MemoryStore(cwd).load_indexes_text()` and truncating at `MAX_MEMORY_INDEX_CHARS` (8K).
- `coding_agent/tools/filesystem.py::_read_file`: the generic binary error message now also points at the office tools (`read_excel_sheet`, `read_word_text`, `read_pdf_text`, `read_pptx`, `edit_notebook_cell`) instead of only suggesting `xxd`/`file`/`hexdump`.
- `coding_agent/tools/web.py::web_search`: response shape changed from `list[{title,url}]` to `{results: list, _meta: {backends_tried, hint?}}`. Backward compat: render.py handles both shapes; the legacy list form is still parsed if encountered.
- `coding_agent/interface/render.py::_summarize_tool_result`: web_search summary now shows `[backend]` tag when meta is present (e.g. `result1 · result2 [brave]`).

## [0.5.0] - 2026-05-14

Focus of this release: cut weak-tier round-trip cost (batch + outline tools), make the planner / validator harder to fool, and unify the ad-hoc grounding supervisor onto a small per-turn observation framework that future checks can hang off. Strong-tier behaviour is unchanged for every code path that isn't gated on `provider.resolved_tier() == "weak"`.

### Added
- `coding_agent/tools/filesystem.py`: new `read_files(paths, offset?, limit?)` tool — reads up to 10 files in one call, returning a JSON array of `{path, content, total_lines}` or `{path, error}` per entry. Designed to replace N sequential `read_file` calls when correlating content across files.
- `coding_agent/tools/filesystem.py`: new `file_outline(path)` tool — uses `ast` to extract classes (with bases + methods), top-level functions, and imports from a Python file with line numbers. Cheap structural alternative to `read_file` when you only need to locate a symbol. Non-Python files raise a clear error pointing back to `read_file`.
- `coding_agent/core/coordinator.py`: `_try_parse_plan_json()` — tolerant planner-output parser that strips ```` ```json ```` fences, peels prose preambles, and falls back to outermost-`{…}` bracket matching before declaring the response unparseable. Used by the new planner retry path.
- `coding_agent/core/coordinator.py`: planner JSON retry — when the first planner response is unparseable, the coordinator re-prompts once with a stricter "RETURN JSON ONLY" reminder before falling back to `is_simple=True`. Weak models often produce a markdown-wrapped or prose-prefixed response on the first attempt but recover on the second.
- `coding_agent/core/coordinator.py`: investigation synthesis pass — when `investigate_only` is set and ≥2 research workers produced output, a final synthesis LLM call consolidates the joined findings into one coherent answer keyed off the user's original prompt. The previous behaviour (join + cross-worker dedup) is the fallback when synthesis fails. Strong-tier "single worker" investigations bypass this entirely.
- `coding_agent/core/coordinator.py`: concrete pytest validator — `_run_concrete_validators()` runs `python -m pytest -x -q tests/` (120s timeout) when the workspace has a `tests/` directory with `test_*.py` files. If pytest fails, `_validate` short-circuits to `passed=False` with the tail of pytest output as feedback, bypassing the LLM-as-judge step. If pytest passes, the evidence is appended to the validator prompt so the judge can rely on it.
- `coding_agent/core/runtime.py`: `_ToolObservations` dataclass + `_check_final_answer_grounding()` framework — replaces the ad-hoc `grep_had_matches` / `read_was_called` locals with a per-turn observation object that future grounding checks can hang off without growing more flags. Emits `grounding_retry` events with a stable `reason` tag.
- `coding_agent/core/runtime.py`: new grounding check `claimed_success_but_bash_failed` — fires when the model's final answer contains a success phrase (English or Chinese: "tests passed", "compiled successfully", "測試通過", …) but the most recent bash returncode was non-zero. Injects a supervisor message and forces one retry iteration.
- `coding_agent/memory/prompting.py`: tier-aware system prompt — when `provider.resolved_tier() == "weak"`, a new `# Grounding rules (local-model tier)` section is added with explicit "read before answering", "cite filename:line", "use batch tools" guidance. Strong-tier prompts are unchanged.
- `tests/test_improvements.py`: 34 new tests covering the batch tools, file_outline AST extraction, planner parser edge cases, weak-tier prompt section, grounding check matrix (grep/read + bash returncode), concrete validator detection, and tool-observation recording.

### Changed
- `coding_agent/tools/filesystem.py`: `edit_file` now rejects `old_string == new_string` upfront (was previously a silent no-op write). Also defends in depth by comparing pre/post text and refusing to claim success if a non-identical replacement still produced no change.
- `coding_agent/core/runtime.py`: `_record_tool_observation()` centralises grounding-state updates from each tool call (grep matches, read paths, write paths, edit paths, bash returncode) into one method. Replaces the inline if/elif chain in the previous run loop.
- `coding_agent/core/coordinator.py`: investigate-only flow now filters empty worker outputs before joining, so a single non-empty worker still takes the synthesis-bypass path correctly.

## [0.4.2] - 2026-05-14

Focus of this release: making the agent usable on weak-tier local models (Qwen3-class, Llama, Mistral, …) without sacrificing strong-tier behaviour, plus a rewrite of the interactive shell.

### Added
- `coding_agent/interface/eval.py`: new evaluation runner that executes a prompt suite (`tests/eval_prompts.yaml`) against a configured model and writes a single JSON file capturing per-prompt tool calls, file reads, iterations, and final text. Designed to run on the target machine (e.g. a Qwen3-32B box) and ship the JSON back for offline diffing — the runner does NOT auto-judge correctness.
- `coding_agent/config/settings.py`: `provider.intelligence_tier` config field (`auto` / `strong` / `weak`) and `resolve_intelligence_tier()` helper. `auto` infers from model name (Qwen / Llama / Mistral / Phi / Gemma / Yi / DeepSeek-distill → weak; everything else → strong) so existing strong-tier deployments are unaffected.
- `coding_agent/core/coordinator.py`: investigation-only execution path. When a weak-tier model is configured AND the planner returns `is_simple=True` on an investigation prompt (`why`, `where is`, `how does`, `list every`, `為什麼`, `哪裡`, …), the plan is forced through research-only flow and the research output IS the final answer — skipping the work + validate phases that weak models often hallucinate through.
- `coding_agent/core/coordinator.py`: `looks_like_investigation(prompt)` is now a module-level helper used by both the coordinator and the runtime grounding supervisor; recognises English and Chinese investigation cues plus `list/count/show/find every|each|all`.
- `coding_agent/core/runtime.py`: grounding supervisor for weak-tier investigation prompts. If the model called `grep_search` and got matches but never called `read_file` before producing a final answer, the runtime injects a forced-read system message and re-runs one iteration. Emits a `grounding_retry` event with `reason=grep_matched_but_no_read`. Fires at most once per turn and only for weak-tier + investigation prompts, so strong-tier behaviour is unchanged.
- `coding_agent/core/response_dedup.py`: new module that collapses multi-pass repetition in model output (Qwen3 frequently emits the same answer 2–3 times with slight rewording). Uses paragraph-similarity period detection — a single match is treated as coincidence; ≥2 matching pairs are needed to confirm a real repetition pattern. Conservative defaults (≥600 chars, 3–60 paragraphs, similarity ≥ 0.65).
- `coding_agent/core/response_dedup.py`: conservatism guard — refuses to dedup when the "last pass" would be < 15% of the total response. Prevents truncating a long worker answer down to a short closing summary when the two share lead-in phrasing.
- `tests/eval_prompts.yaml`: 133-line prompt suite covering search / read / write / investigation / multi-step categories, used by `yucode eval`.

### Changed
- `coding_agent/interface/cli.py`: interactive shell rewritten on top of `prompt_toolkit`. Multi-line input (Alt+Enter inserts newline, Enter submits, Enter on an open completion popup just accepts), Ctrl+C now clears the buffer when text is present and raises `KeyboardInterrupt` only on an empty line, persistent history at `~/.yucode/history`, and an `@file` completer that hides `__pycache__` / `.git` / `.DS_Store` / `.mypy_cache` / `.ruff_cache` / `.pytest_cache` unless the user explicitly types a `.` prefix.
- `coding_agent/interface/render.py`: `StreamingTextDisplay` rewritten — intermediate `assistant_delta` text now renders on stderr (alongside the spinner) and `finalize()` ERASES the streamed lines from the terminal on TTY, computing physical rows with wrap-awareness. The final polished answer is still printed separately on stdout by the chat loop. Non-TTY (redirected output) preserves the streamed text as a flat transcript.
- `coding_agent/core/coordinator.py`: cross-worker output dedup. When the planner splits one question into multiple work / research tasks and each worker produces an overlapping answer, joining them with `\n\n` previously surfaced as multi-pass repetition. Both `_run_research` (investigation-only) and the work-result joiner now run `dedup_repetitive_response()` on the joined text.
- `coding_agent/core/providers.py`: `_TextToolCallFilter._holdback()` replaces the fixed `_MAX_OPEN_LEN` reserve. The buffer tail is held back only when its suffix is a proper prefix of an open tag — short text that clearly isn't a tag prefix is emitted immediately, removing per-chunk latency on streaming.
- `coding_agent/core/providers.py`: OpenAI-compatible streaming parser now tolerates the final `choices: []` chunk that providers send when `stream_options.include_usage` is enabled, processing usage from that chunk without crashing on the missing delta.
- `coding_agent/interface/eval.py`: per-prompt outer retry with 30-second wait when the provider raises a transient gateway error (`502` / `503` / `504` / `timeout`). Eval keeps going across prompts on non-transient errors as before.
- `coding_agent/tools/filesystem.py`: `edit_file` now rejects ambiguous matches — if `old_string` appears more than once and `replace_all` is `false`, it raises a clear error instructing the caller to either pass `replace_all: true` or extend `old_string` with surrounding context.

### Fixed
- `coding_agent/config/settings.py`: `_deep_merge` no longer lets an empty overlay value wipe a non-empty base value. An empty `api_key` in `settings.local.yml` no longer shadows the real key from `settings.yml`.
- `coding_agent/tools/filesystem.py`: `_py_grep_lines` now stores relative paths in its result list (previously absolute), matching the rest of the search pipeline so downstream rendering and dedup work correctly.

## [0.3.5] - 2026-04-24

### Added
- `coding_agent/security/bash_validation.py`: new module that narrows the gap vs. claw-code's `bash_validation.rs`:
  - `CommandIntent` enum + `classify_command()` — tags commands as read-only / write / destructive / network / process / package / system-admin / unknown
  - `extract_first_command()` — strips leading `KEY=val` env-var prefixes so classification/read-only detection is not fooled by `FOO=bar rm -rf /`
  - `check_sed_in_place()` — warns on `sed -i` (silent in-place edit)
  - `check_path_traversal()` — warns on `../` and on write/destructive commands that target system paths (`/etc/`, `/usr/`, `/var/`, `/dev/`, …)
- `coding_agent/security/__init__.py`: re-exports `CommandIntent` and `classify_command`
- `tests/test_security.py`: 13 new tests covering the above

### Changed
- `coding_agent/security/safety.py::check_bash_safety()` now also runs `check_sed_in_place` and `check_path_traversal` as part of the pipeline
- `coding_agent/security/permissions.py::_is_read_only_command()` now strips leading `KEY=val` env-var tokens before matching — `FOO=bar ls` is now recognised as read-only, `FOO=bar rm -rf /tmp` is still rejected

## [0.3.4] - 2026-04-22

### Fixed
- `coding_agent/core/providers.py`: stream-stall timeout in `_iter_stream()` now raises `ProviderError ... from None` so the exception chain doesn't point at the spurious `queue.Empty` (ruff B904)
- `coding_agent/interface/render.py`: spinner animation thread now uses `contextlib.suppress(Exception)` around `_redraw()` instead of `try/except/pass` (ruff SIM105)

### Changed
- CI lint job (`ruff check coding_agent/ tests/`) is green again — both errors above previously failed the pipeline

## [0.3.3] - 2026-04-11

### Added
- `ContextWindowExceededError` and `RetriesExhaustedError` in the structured error hierarchy
- Sub-agent execution timeout (default 5 minutes) with structured timeout/error responses
- Web fetch safety limits: 5 MB response cap, 10-redirect cap, 30s timeout
- `CLAUDE.md` with repo-level commit authorship policy

### Changed
- All provider HTTP failure paths now raise `ProviderError` / `RetriesExhaustedError` instead of bare `RuntimeError`
- Hardened tools with size limits, safety patterns, and output budgets
- Fixed text-based tool call parsing and related quality issues
- `agent` tool now uses `tool_error_response` helper and narrows `BaseException` to `Exception` so `KeyboardInterrupt`/`SystemExit` propagate
- Tightened provider-parsing tests to assert on `ProviderError` directly

## [0.3.0] - 2026-04-08

### Added
- **Hybrid provider mode** (`streaming_mode: hybrid`): automatically retries with non-streaming when streaming returns an empty response, resolving connectivity failures on providers with unreliable SSE support
- `streaming_mode` config field with values `stream`, `no_stream`, and `hybrid` (default); backward-compatible with existing `provider.stream` boolean
- Home-based state management: sessions, audit logs, metrics, todos, exports, plugins, archives, and checkpoints now live under `~/.yucode/projects/<workspace_key>/` instead of `<workspace>/.yucode/`
- `state_dir()` and `workspace_key()` helpers in config for deterministic per-project state paths
- 12 new tests covering hybrid fallback, streaming mode coercion, workspace key generation, and state directory resolution

### Changed
- Provider `complete()` refactored into `complete()` dispatcher + `_do_complete()` so both modes share the same HTTP/retry logic
- `yucode doctor` reports the home-based state directory instead of checking for workspace `.yucode/`
- `yucode init` creates state under `~/.yucode/projects/` instead of `<target>/.yucode/`
- Default `streaming_mode` is `hybrid` in bundled config and new installations

### Fixed
- Providers that silently fail on streaming now automatically retry via non-streaming in hybrid mode instead of returning an empty response

## [0.2.3] - 2026-04-08

### Added
- Added doctor smoke tests that verify provider probing reports streaming failures more clearly

### Changed
- Upgraded `yucode doctor` to probe both non-streaming and streaming provider modes instead of only checking whether an API key exists

### Fixed
- Reduced duplicate warning noise during `yucode chat` failures by routing provider diagnostics through CLI events
- Improved diagnostics for providers that pass basic config checks but fail to return usable streaming output

## [0.2.2] - 2026-04-08

### Added
- Added focused provider parsing regression coverage for alternate content shapes, usage schemas, and streaming edge cases

### Changed
- Improved CLI troubleshooting guidance for empty provider responses and environment setup
- Broadened OpenAI-compatible response parsing to accept block-style content and alternate token usage fields

### Fixed
- Fixed silent `yucode chat` failures where compatible providers could return a blank response with `0` token usage
- Surfaced clearer runtime and CLI diagnostics when provider configuration or streaming output is incompatible

## [0.2.1] - 2026-04-07

### Added
- Registry-backed Task, Worker, Team/Cron, and LSP runtime modules to close major claw-code parity gaps
- Lane event, stale-branch, task packet, policy engine, green contract, branch lock, recovery recipe, and summary compression support
- Release automation via GitHub Actions and a `Containerfile` for reproducible packaging
- Behavioral parity coverage in `tests/test_parity_harness.py`

### Changed
- Expanded CLI parity with `doctor`, `system-prompt`, `version`, and broader `--output-format json` support
- Hardened MCP lifecycle handling with reconnection attempts, timeout handling, resource reads, and discovery reporting
- Tightened permission handling with explicit prompt-mode gating, rule evaluation, hook overrides, and workspace-scoped tool enforcement
- Updated CI so lint, tests, packaging, and doctor checks pass on clean runners

### Fixed
- Corrected prompt-mode permission behavior so approval flows no longer auto-allow dangerous tools
- Fixed the doctor workflow to bootstrap a clean CI workspace without requiring real user credentials
- Cleaned release/build behavior so generated artifacts stay out of the repo root and packaging remains reproducible

## [0.2.0] - 2026-04-05

### Added
- Structured error hierarchy (`AgentError`, `ProviderError`, `McpError`, etc.)
- Environment-variable based secret management (`YUCODE_API_KEY`)
- Secret scanning and automatic redaction in tool outputs
- Audit log persistence to `.yucode/audit/` (append-only JSONL)
- PreCompact / PostCompact hooks for context management
- Session archival before compaction (`.yucode/archives/`)
- Checkpoint / resume mechanism (`AgentRuntime.checkpoint()`)
- Structured logging with text/JSON formats
- Safety bypass governance (`YUCODE_DANGEROUS_MODE`)
- Background bash sandbox wrapping (closing security gap)
- `compact_strategy` config: `heuristic` (default) or `llm`
- `error_strategy` config: `strict` or `resilient` (default)
- Databook MCP server for Excel/CSV data analysis and PPTX generation
- Template-based PPTX creation tool
- PII detection patterns in safety module

### Changed
- API key no longer stored in `config.yml` by default; resolved from env vars
- MCP errors now raise `McpError` with server name context
- Tool errors return structured JSON with `error_code`, `recoverable`, `suggestion`
- Version now sourced from `importlib.metadata` (single source of truth)
- Updated architecture docs to match actual package layout

### Fixed
- Background bash processes now apply sandbox wrapping (previously bypassed)
- MCP server failures gracefully disable the server instead of crashing

### Security
- Added `.gitignore` to prevent accidental commit of secrets
- Removed hardcoded API key from `config.yml`
- Added secret pattern scanning (API keys, tokens, private keys, JWTs)
- Added PII detection (email, phone, SSN patterns)

## [0.1.0] - 2024-12-01

### Added
- Initial release with ReAct agent loop, tool registry, MCP support
- Permission policy with 5 ordered modes
- Session compaction with heuristic token estimation
- VS Code bridge and HTTP server
- Plugin system with manifest-based discovery
- Sandbox and filesystem isolation
