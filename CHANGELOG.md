# Changelog

All notable changes to this project will be documented in this file.

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
