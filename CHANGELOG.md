# Changelog

All notable changes to this project will be documented in this file.

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
