# Changelog

All notable changes to this project will be documented in this file.

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
