# The Python implementation, summarised for the TypeScript port

Written 2026-09-12, against the tree tagged `python-final`. Every `file:line`
below points into that tag; after the reset the same tree lives, untracked, in
`_legacy_python/`.

**Read this first, before any work package.** It is meant to be enough on its
own for most of the port. Where it is not, it tells you which file to open —
and after the reset, `_legacy_python/` is the only place that file exists.

Companion material:

- `docs/REWRITE_PLAN.md` — what to build, in what order, and why. Its §0.0
  corrections section wins over its own body wherever they disagree.
- `tests/golden/harness/**` — machine-readable baselines captured from this
  implementation. Every claim in this document about a schema, a key name or
  an output shape has a file there that pins it. Assert against the file, not
  against this prose.
- `tests/golden/office/**` — the domain layer's behavioural oracle.

A note on how to read the Python. Roughly a fifth of it was written against an
observed failure, and those places carry a comment or a commit message saying
which. Keep that behaviour and its test. Another chunk was transliterated from
claw-code and never executed; §21 lists it, and none of it gets ported.

---

## 1. Module map

LOC as at `python-final`. GEN = generic harness, DOM = firm-specific domain,
DEAD = not reachable with the shipped defaults.

### `core/` — the loop and everything it composes

| File | LOC | Tag | What it is |
|---|---|---|---|
| `runtime.py` | 1980 | GEN | `AgentRuntime`: the turn loop, tool execution pipeline, supervisors, reminders, compaction triggers. §5 and §6. |
| `providers.py` | 1403 | GEN | The single OpenAI-compatible provider and all its quirks. §9. |
| `coordinator.py` | 950 | GEN | plan→research→work→validate. **v2, not v1** — but `looks_like_investigation`, `is_complex_prompt` and `classify_reasoning_effort` are needed by the v1 loop. |
| `session.py` | 399 | GEN | `Session`, `Message`, `ToolCall`, `Usage`, request-time pruning, save/load. §10. |
| `errors.py` | 144 | GEN | Typed errors and the `tool_error_response` envelope. |
| `response_dedup.py` | 120 | GEN | Collapses a model that repeats itself verbatim in one reply. |
| `shellexec.py` | 35 | GEN | The Windows `cmd.exe /S /C` wrapper. Small and load-bearing — §17. |
| `atomic_io.py` | 21 | GEN | tmp-write + `os.replace`. |
| `task_registry.py` | 176 | DEAD | |
| `team_cron_registry.py` | 175 | DEAD | |
| `worker_registry.py` | 264 | DEAD | |
| `task_packet.py` | 65 | DEAD | |
| `summary_compression.py` | 106 | DEAD | |

### `tools/`

| File | LOC | Tag | What it is |
|---|---|---|---|
| `__init__.py` | 784 | GEN | `ToolRegistry`, `ToolSpec`, `ToolDefinition`, name resolution, the path boundary, background/subagent tables, the 32 extended stubs. |
| `filesystem.py` | 1051 | GEN | read/write/edit/glob/grep/list/read_files/file_outline. |
| `patch.py` | 362 | GEN | `apply_patch` — the Codex envelope. |
| `shell.py` | 336 | GEN | `bash`, background tasks, env scrubbing, output spill. |
| `web.py` | 503 | GEN | `web_fetch`, `web_search`. |
| `agent_tool.py` | 239 | GEN | The `agent` sub-agent tool. |
| `memory_tools.py` | 201 | GEN | Five `memory_*` tools. |
| `misc.py` | 155 | GEN | `todo_write`, `load_skill`, `config_read`, `structured_output`, MCP resources. |
| `notebook.py` | 38 | GEN | `edit_notebook_cell`. |
| `office.py` | 1245 | DOM | All 22 domain tools. |
| `text_metrics.py` | 328 | DOM | Font resolution, text wrapping, capacity estimation. |
| `pdf_tables.py` | 261 | DOM | pdfplumber word extraction and the period table. |
| `databook_metrics.py` | 170 | DOM | Stage-column location in an FDD databook. |

### Everything else

| File | LOC | Notes |
|---|---|---|
| `interface/cli.py` | 2418 | 21 subcommands plus the REPL. §18. |
| `interface/render.py` | 1092 | Event renderer, spinner, markdown→ANSI, banner. |
| `interface/eval.py` | 404 | The prompt-suite runner. |
| `interface/commands.py` | 298 | Slash-command parsing, `@file` inlining, git helpers. |
| `interface/bridge.py` | 285 | VS Code JSONL bridge over stdio. |
| `interface/init_workspace.py` | 278 | `yucode init`. |
| `interface/server.py` | 230 | HTTP + SSE (aiohttp). |
| `interface/memory_bootstrap.py` | 98 | `yucode init-memory`. |
| `memory/prompting.py` | 520 | Prompt assembly and instruction-file discovery. `_office_files_section` at `:410` is DOM prose inside a GEN file. |
| `memory/compact.py` | 485 | Compaction, token estimation. |
| `memory/store.py` | 245 | The markdown memory store. |
| `memory/skills.py` | 198 | Skill discovery and the 4-tier summary budget. |
| `security/permissions.py` | 585 | Modes, rules, shell-command decomposition. §12. |
| `security/safety.py` | 216 | Destructive-command patterns. |
| `security/env_scrub.py` | 187 | Child-process environment filtering. |
| `security/sandbox.py` | 224 | bwrap. Linux-only, **not ported**. |
| `security/bash_validation.py` | 165 | DEAD. |
| `config/settings.py` | 1072 | Every dataclass, discovery, merge, coercion. §4. |
| `config/simple_yaml.py` | 202 | A stdlib YAML subset. The port uses the `yaml` package; read this only for merge and coercion semantics. |
| `config/trust.py` | 153 | The repo-local-config trust gate. |
| `hooks/__init__.py` | 278 | §13. |
| `plugins/__init__.py` | 366 | `plugin.json` plugins. |
| `plugins/mcp.py` | 448 | The stdio MCP client. §14. |
| `plugins/mcp_servers/_protocol.py` | 201 | The MCP server side, incl. `_guard_paths`. |
| `plugins/mcp_servers/office_mcp.py` | 84 | Bridges the 22 office tools. |
| `plugins/mcp_servers/finance_mcp.py` | 176 | yfinance. **Not ported** — needs network the box refuses. |
| `observability/metrics.py` | 484 | Audit log, metrics, retention sweep. §16. |
| `observability/logging.py` | 66 | No caller. DEAD. |
| Root | | `diagnose_env.py` 530, `diagnose_agent.py` 1132, `test_connection.py` 814, `yucode.cmd`, `yucode.bat`. |

`build/lib/` is a stale setuptools artifact. Ignore it.

### `tools/golden/` — the capture scripts (added during M0, not part of the agent)

| File | What it is |
|---|---|
| `capture_harness_baselines.py` | Writes `tests/golden/harness/`. Byte-stable; `--check` diffs. |
| `capture_office_goldens.py` | Writes `tests/golden/office/`. Subcommands `capture`, `check`, `vendor-fonts`. |
| `fixtures.py` | Builds the synthetic Office and PDF inputs, and proves each one deterministic. |
| `cases.py` | The 95-case table, each with the reason it exists. |
| `projections.py` | Six lxml extractors for the write tools. |
| `fonts.py` | Vendoring, the resolver patch, and the per-glyph measurement. |

These are not imported by the agent and are not packaged
(`pyproject.toml` includes only `coding_agent*`). They live at the repo root so
that the `python-final` tag — and therefore `_legacy_python/` — contains them,
and a later session can re-run them against the Python if a baseline is ever
in doubt.

---

## 2. Entry points

There is **no `coding_agent/__main__.py`**. `python -m coding_agent` fails.
The three ways in:

- `yucode` — console script, `pyproject.toml:60` → `coding_agent.interface.cli:main`
- `python -m coding_agent.interface.cli` — guard at `cli.py:2417`
- `yucode.cmd` — sets `PYTHONPATH=%~dp0` and calls `python -m`

In the port there is one launcher, `yucode.cmd` → `node dist/cli/main.js`.

### Subcommands (`build_parser`, `cli.py:61-181`, `prog="yucode"`)

Global flags, valid **before** the subcommand (`cli.py:63-68`):
`--config`, `--workspace`, `--model`, `--permission-mode`.
Bare `yucode` enters the REPL (`handle_default`, `cli.py:69`, `:987`).

| Subcommand | Flags | Line |
|---|---|---|
| `init-config` | `--defaults` / `-y` | 72 |
| `show-config` | **none** — prints the raw file (`:519-522`) | 75 |
| `config-path` | none | 76 |
| `bridge` | none | 77 |
| `trust` | `--workspace --remove --list` (dest `list_trusted`) | 79 |
| `init` | positional `target` (default `.`) | 88 |
| `init-agents` | `target --force --filename` | 92 |
| `init-memory` | `--force` | 98 |
| `chat` | `prompt? --workspace --json --model --allowed-tools --permission-mode --resume --output-format` | 102 |
| `mcp-list` | `--workspace` | 113 |
| `mcp-add` | `name command args... --transport --workspace` | 117 |
| `mcp-remove` | `name --workspace` | 125 |
| `mcp-validate` | `--workspace` | 130 |
| `mcp-preset` | `preset_name? preset_args... --workspace` | 134 |
| `skills` | `--workspace --output-format` | 140 |
| `status` | `--output-format` | 145 |
| `system-prompt` | `--workspace` (dest **`cwd`**) `--date --output-format` | 149 |
| `version` | `--output-format` | 155 |
| `doctor` | `--workspace --output-format` | 159 |
| `serve` | `--host --port --workspace` | 164 |
| `eval` | `--suite` (required) `--out` (required) `--workspace --only --label` | 170 |

`doctor` is **not offline**: three live provider probes at `cli.py:2277-2312`.

### Headless JSON (`chat --json`, built at `cli.py:1154-1164`)

```
final_text, stop_reason, session_id, iterations,
usage {…, total_tokens}, usage_is_incomplete, events[]
```

Printed with `indent=2`. There is deliberately no `total_cost_usd`
(`cli.py:1160-1162`). `stop_reason` comes from `_stop_reason()`
(`cli.py:1105`), which anchors on text markers the loop writes.
`_FAILURE_STOP_REASONS = {provider_error, stuck, max_tool_calls,
max_iterations, empty_response, incomplete}` (`cli.py:1099`) drive exit code 1
(`cli.py:1188`).

Other JSON shapes worth keeping identical:

| Command | Keys | Line |
|---|---|---|
| `status` | `provider model permission_mode max_iterations max_coordinator_retries orchestration_mode mcp_server_count tools_allowed tools_disabled` | 2168 |
| `system-prompt` | `system_prompt workspace date` | 2201 |
| `version` | `version` | 2211 |
| `skills` | `skills[{name,description,path}]` | 2147 |
| `doctor` | `overall checks[{name,status,summary}] workspace` | 2347 |

`doctor`'s check names: `ripgrep`, `config_file`, `config_parse` (error only),
`api_key`, `state_dir`, `mcp_servers` (conditional), `provider_non_streaming`,
`provider_streaming`, `provider_tool_calling` (conditional), `sandbox`,
`workspace_python_project` (`cli.py:2225-2337`).

`eval --out` payload keys: `version label timestamp workspace git_sha model
intelligence_tier_config intelligence_tier_resolved orchestration_mode
max_iterations max_coordinator_retries max_tool_calls suite verdicts_total
verdicts_passed prompts_failed prompts_needing_human_review results`
(`eval.py:362-381`). `timestamp` and `git_sha` make it non-byte-stable —
compare verdicts, not bytes.

---

## 3. Event vocabulary

The wire format shared by `--json`, the bridge, the server and the renderer.
Emitted through one `event_callback`.

`provider_info` · `iteration_started` · `compaction` · `auto_compaction` ·
`reasoning` · `grounding_retry` · `todo_gate_retry` · `tool_call` ·
`tool_result` · `dedup_limit` · `stuck_exit` · `completed` · `error` ·
`assistant_delta` · `usage` · `warning` · `fallback`

The port adds `turn_started`, `turn_ended`, `background_task`, `subagent`.
Nothing existing may be renamed — `tests/test_event_handler.py` (40 cases) and
the VS Code extension both key off these strings.

---

## 4. Configuration

### Discovery and merge

`discover_config_paths` (`settings.py:522`), in order:

```
~/.yucode/settings.yml           (seeded from the bundled default if missing)
~/.yucode/settings.json
<ws>/.yucode/settings.yml
<ws>/.yucode/settings.json
<ws>/.yucode/settings.local.yml
<ws>/.yucode/settings.local.json
```

plus the workspace `mcp.yml` (`settings.py:462-486`). `--config` replaces the
first entry (`resolve_config_path`, `settings.py:442`).

`_deep_merge` (`settings.py:487`) has three rules, and all three matter:

1. dict + dict recurses;
2. list + list **concatenates** (it does not replace);
3. an overlay value of `""` or `None` never shadows a non-empty base value —
   the reason an empty `api_key` in `settings.local.yml` does not wipe the
   real one.

Repo-local overlays pass the trust gate (`config/trust.py`): `hooks` and `mcp`
are stripped unless the directory is trusted, and `permission_mode` may only
tighten.

A read-only or offline `HOME` must not be fatal: `load_app_config`
(`settings.py:535`) catches `OSError` around seeding and reading, warns, and
continues on bundled defaults.

### Env overrides

`YUCODE_API_KEY` (beats the file, `settings.py:81`) · `YUCODE_PROVIDER_NAME` ·
`YUCODE_BASE_URL` · `YUCODE_MODEL` · `YUCODE_HOME` · `YUCODE_DANGEROUS_MODE` ·
`YUCODE_MCP_ROOT` · `YUCODE_AUTO_COMPACT_INPUT_TOKENS` ·
`YUCODE_STATE_RETENTION_MB` / `_DAYS` · `BRAVE_API_KEY`.

`YUCODE_HOME` is read **once, at import time**, into `_HOME_YUCODE`
(`settings.py:58`). Anything that sets it must do so before importing.

### `ProviderConfig` (`settings.py:91`)

| Field | Default | Note |
|---|---|---|
| `name` | `""` | |
| `type` | `"openai_compatible"` | Only one implementation exists. |
| `base_url` | `""` | |
| `api_key` | `""` | |
| `model` | `""` | |
| `chat_path` | `"/chat/completions"` | |
| `append_chat_path` | `True` | |
| `api_version` | `""` | Non-empty ⇒ Azure deployment URL + `api-key` header, overriding `chat_path`/`append_chat_path` entirely. |
| `verify_tls` | `True` | |
| `ca_bundle` | `""` | Preferred over `verify_tls: false`. |
| `stream` | `True` | |
| `streaming_mode` | `"hybrid"` | `hybrid` / `no_stream` / `stream` |
| `temperature` | `0.0` | |
| `max_tokens` | `0` | 0 means **omit the parameter**. |
| `request_timeout_seconds` | `90` | |
| `intelligence_tier` | `"auto"` | |
| `reasoning_effort` | `""` | `""` omit · `"auto"` run the heuristic · anything else sent verbatim. Falls back to `extra_body["reasoning_effort"]` (`configured_reasoning_effort`, `settings.py:135`). |
| `extra_headers` | `{}` | |
| `extra_body` | `{}` | |
| `omit_params` | `[]` | |

`resolved_tier()` → `resolve_intelligence_tier` (`settings.py:32`): `weak` if
the model name contains any of `qwen llama mistral mixtral phi- phi3 phi4
gemma yi- yi: codellama code-llama starcoder deepseek-r1-distill
deepseek-coder-v2-lite`; unknown models default to `strong`.

### `RuntimeOptions` (`settings.py:154`) — all 30 fields

```
permission_mode            workspace-write
max_iterations             25          (bundled config.yml raises it to 32)
max_worker_steps           20
max_coordinator_retries    3
orchestration_mode         auto
parallel_workers           False
max_tool_calls             80
dedup_tool_threshold       3           (weak tier drops it to 2 at runtime)
auto_save_session          True
auto_resume_latest         True
compact_preserve_recent    4
compact_token_threshold    60000       (bundled config.yml sets 10000)
compact_strategy           heuristic
error_strategy             resilient
shell_timeout_seconds      30
shell_env_filter_secrets   True
shell_env_exclude_patterns []
shell_env_allowlist        []
include_git_context        True
config_dump_in_prompt      True
prune_tool_results         True
prune_keep_last_turns      3
prune_soft_trim_threshold  4000
prune_soft_trim_head       1500
prune_soft_trim_tail       1500
prune_hard_clear_turns     10
auto_background_on_timeout False
todo_nudge_enabled         True
todo_nudge_turns_since_write 3
todo_nudge_min_gap_turns   5
todo_gate_enabled          False
todo_gate_max_fires_per_prompt 2
agents_md_discovery_enabled  True
skill_discovery_enabled      True
```

`YUCODE_AUTO_COMPACT_INPUT_TOKENS` defaults to 100000.

The measurement behind `compact_token_threshold`: at 10k the prompt cache hit
rate was 23%, at 120k it was 36% (IMPROVEMENT_PLAN §10). Compaction and
request-time pruning both rewrite the front of the request, which is what the
cache matches on, so this is a cache setting as much as a context one
(`session.py:299-311`).

### Other config blocks

`ToolOptions` (`settings.py:224`): `allowed`, `disabled`,
`extended_tools_enabled` (default `False`).
`McpServerConfig` `:235` · `VscodeOptions` `:246` · `HookOptions` `:253` ·
`PluginOptions` `:265` · `PermissionRuleConfig` `:271` · `SandboxOptions`
`:278` · `LoggingOptions` `:287` · `AuditOptions` `:294`.

`AppConfig.to_control_dict()` (`settings.py:319`) is the full serialisation;
`as_prompt_safe_dict()` (`:313`) is the same with `api_key` replaced by `***`,
and is what goes into the system prompt when `config_dump_in_prompt`.

**Baselines:** `tests/golden/harness/config_control_dict.{bundled,workbench,dataclass_defaults}.json`.

### State directory

`state_dir()` (`settings.py:67`):
`$YUCODE_HOME|~/.yucode/projects/<workspace_key>/`, where `workspace_key` is
`sha256(str(workspace.resolve()))[:12]` (`settings.py:61`). A legacy
`<ws>/.yucode` is used instead when it exists and the home-based one does not.

Subdirectories: `sessions archives checkpoints audit metrics tool_output
bash_output exports plugins`. Files: `todos.json`, `mcp.yml`.

The hash input is the resolved path **string**. A port that normalises
separators differently produces a different directory and silently loses every
saved session. `tests/golden/harness/state_layout.json` pins four samples,
including a Windows path.

---

## 5. The turn loop

`AgentRuntime._run_turn` (`runtime.py:764`). Stage order is the spec.

**Setup, once per turn**

1. `_ensure_plugins_initialized()`; emit `provider_info`.
2. `discover_project_context(workspace, date, include_git_context,
   instruction_files)` (`runtime.py:783`).
3. Seed `_agents_md_checked` with the instruction files already in the prompt
   and `_announced_skill_files` with the skills already summarised, so the
   discovery reminders only ever surface something new (`runtime.py:789-796`).
4. `PromptAssembler(...).render()`, stored on `session.system_prompt` —
   storage only; `provider_messages()` still takes the prompt as an argument
   (`runtime.py:798-808`).
5. Weak-tier investigation prefix: when `resolved_tier() == "weak"` **and**
   `looks_like_investigation(prompt)`, append a bracketed directive requiring
   at least one `grep_search`/`read_file` before answering
   (`runtime.py:812-823`).
6. Append the (possibly prefixed) user message.
7. `dedup_threshold = max(2, configured - 1)` on the weak tier
   (`runtime.py:833-836`).
8. Resolve per-turn `reasoning_effort` once: an explicit level wins; `auto`
   runs `classify_reasoning_effort` (`runtime.py:1013-1030`). This is a fix —
   the configured value used to be read as an on/off gate and then discarded,
   so a user asking for `high` got whatever the heuristic chose.

**Per iteration, `1..max_iterations`**

1. Emit `iteration_started`; increment `_turns_since_todo_write` and
   `_turns_since_todo_nudge`.
2. Break if the tool-call budget was exhausted last iteration.
3. `should_compact(...)` → `compact()` → emit `compaction`.
4. `provider.complete(session.provider_messages(system_prompt,
   pruning=runtime), tools.definitions_for_provider(), ...)`. On exception:
   `strict` re-raises, `resilient` sets `final_text` to
   `[Provider error: …]`, emits `error` with `category: provider_error`, and
   returns.
5. Record `finish_reason`, usage, `_last_response_input_tokens`. Emit
   `reasoning` if the gateway returned chain-of-thought — it is **not** stored
   in the session and never sent back (`runtime.py:934-943`).
6. Append the assistant message.

**When there are no tool calls** (`runtime.py:952`)

1. **Grounding supervisor**, at most once per turn. `_check_final_answer_grounding`
   runs three checks: `grep_matched_but_no_read` (weak tier + investigation),
   `claimed_success_but_bash_failed`, `time_sensitive_no_web_search`. A
   violation appends a `user` message carrying the violation text, emits
   `grounding_retry`, and `continue`s.
2. **Todo gate**, opt-in, capped at `todo_gate_max_fires_per_prompt`. Open
   todos → a `[SYSTEM SUPERVISOR]`-prefixed user message listing up to 10,
   emit `todo_gate_retry`, `continue`.
3. Otherwise: `dedup_repetitive_response(text)`, the empty-response warning
   when text is empty and usage is 0 on iteration 1, emit `completed`, record
   metrics, `_maybe_auto_compact`, return.

**Per tool call** (`runtime.py:1028`)

1. Emit `tool_call`.
2. Canonicalise arguments — `json.dumps(json.loads(args), sort_keys=True,
   separators=(",",":"))` — then key on
   `(name, _content_stable_hash(canonical)[:16])`. Canonicalising is what
   stops `{"a":1,"b":2}` and `{"a": 1, "b": 2}` counting as two different
   calls (`runtime.py:1040-1053`).
3. `count >= dedup_threshold` → a synthetic `dedup_limit` error whose text
   escalates: plain, then "hard-blocked N times", then "FINAL WARNING" at
   `_MAX_STUCK_DEDUP_BLOCKS` (3). Emit `dedup_limit`.
4. Else `tool_call_count >= max_tool_calls` → budget error, set
   `budget_exhausted`.
5. Else: read-only cache hit reuses the stored result; otherwise
   `_execute_tool()` (§6) and `tool_call_count += 1`. Results of read-only
   tools are cached for the rest of the turn.
6. Record grounding observations; `todo_write` resets
   `_turns_since_todo_write`.
7. Read-only streak: incremented for read-only tools, reset by anything else.
   Exactly at `_MAX_CONSECUTIVE_READONLY` (8) an advisory paragraph is
   appended to that one result.
8. `_collect_post_tool_reminders` (`runtime.py:1488`), appended in this order:
   completed background bash tasks, completed background sub-agents, the todo
   nudge, AGENTS.md discovery, skill discovery. Each is a `<system-reminder>`
   block.
9. Append the tool message; emit `tool_result`.

**After the loop**

- `dedup_blocks_this_turn >= 3` → `final_text` = last non-empty assistant text
  plus a `[Stopped: …appears stuck…]` note; emit `stuck_exit`.
- Otherwise a `[Note: Reached the maximum number of {tool call budget|
  iterations} (N).]` note; emit `completed`.
- Both paths: record metrics, persist, `_maybe_auto_compact`.

**`_maybe_auto_compact`** (`runtime.py:1201`) gates on the **most recent**
response's input tokens, not a lifetime sum. The cumulative version
force-compacted on every turn forever once it had crossed the threshold once,
because the sum only grows even after a compaction shrinks the real context.

Depth is tracked through thread-locals in the Python. The port passes `depth`
explicitly through `runTurn`, and sub-agents run in-process as child runtimes.

---

## 6. The tool execution pipeline

`AgentRuntime._execute_tool(tool_name, raw_arguments) -> str`
(`runtime.py:1809`). Takes a **JSON string**; the registry underneath takes a
dict. In order:

1. Unknown tool → `tool_error_response(..., "unknown_tool", recoverable=False)`.
2. `permission_enforcer.check` → `permission_denied`.
3. `permission_policy.authorize(permission, name, tool_input, prompter)` →
   `permission_denied`.
4. `PreToolUse` hook. Denial stops here with `hook_denied`.
5. `json.loads(raw_arguments or "{}")`. On failure the error depends on the
   last `finish_reason`: if it was `length`/`max_tokens`, the message says the
   arguments were **severed by the output cap**, not malformed, with
   `error_code: output_truncated` and advice to split the work — because "fix
   the syntax and retry" is advice that cannot be followed when the same call
   will be cut at the same place (`runtime.py:1860-1886`).
6. `tools.execute(name, arguments)`. `McpError` and any other exception become
   `is_error = True` with `str(exc)` as the output.
7. Merge pre-hook feedback, then run `PostToolUse` or `PostToolUseFailure`.
8. `metrics.record_tool_call` + `metrics.record_tool_audit`.
9. `scan_and_redact_secrets(output)`; a redaction emits a `secret_redacted`
   security event.
10. If the output exceeds `_MAX_TOOL_RESULT_BYTES` (32 KiB): spill the **full**
    text to `state_dir/tool_output/`, then truncate with a note naming the
    spill path. Truncating in place was lossy for every tool but `bash`.
    `_truncate_tool_output` preserves JSON structure via
    `_shrink_json_strings` when the output parses as JSON.
11. `is_error` → `tool_error_response(output, "tool_error", recoverable=True)`.

The error envelope (`errors.py:129-144`) is
`{"error", "error_code", "recoverable", "suggestion"?}`, `json.dumps(indent=2)`.

### `ToolRegistry.execute` (`tools/__init__.py:533`)

```python
def execute(self, tool_name: str, arguments: dict[str, Any]) -> str
```

Raises `KeyError(f"Unknown tool: {tool_name}")`; handler exceptions propagate
uncaught. Name resolution is case- and underscore-insensitive
(`_resolve_name`, `:518`; index built at `:495`), so `WebSearch` and
`web_search` both reach `web_search`. Aliases: `read write edit glob grep ls`.

`definitions_for_provider()` (`:484`) returns
`{"type":"function","function":{"name","description","parameters"}}` in
**registration order**, not sorted.

---

## 7. Tool catalogue

50 tools are registered with the shipped defaults: 28 generic + 22 domain.
Full schemas, permissions and risk levels are in
`tests/golden/harness/tool_definitions.json` — **assert against that file**,
not against the list below.

Note: `tool_search` is documented in `coding_agent/README.md:386` but has no
implementation. It is not registered and there is nothing to drop.

### Generic (28)

| Tool | Required · optional | Perm |
|---|---|---|
| `read_file` | `path` · `offset limit` | RO |
| `read_files` | `paths` (≤10) · `offset limit` | RO |
| `write_file` | `path content` | WW |
| `edit_file` | `path old_string new_string` · `replace_all` | WW |
| `glob_search` | `pattern` · `target_directory` | RO |
| `grep_search` | `pattern` · `path glob` | RO |
| `list_directory` | · `path` | RO |
| `file_outline` | `path` | RO |
| `apply_patch` | `patch` | WW |
| `bash` | `command` · `timeout working_directory run_in_background description` | DFA |
| `web_fetch` | `url` · `prompt` | RO |
| `web_search` | `query` · `allowed_domains blocked_domains` | RO |
| `edit_notebook_cell` | `path cell_index new_source` | WW |
| `agent` | `prompt` · `role allowed_tools run_in_background` | WW |
| `todo_write` | `todos` · `merge` | WW |
| `mcp_list_resources` | `server` | RO |
| `mcp_read_resource` | `server uri` | RO |
| `load_skill` | `name` | RO |
| `config_read` | — | RO |
| `structured_output` | `data` | RO |
| `sleep` | | — **dropped in the port** |
| `config_write` | | — **dropped in the port** |
| `memory_save` | `name description type content` · `scope` | WW |
| `memory_list` | · `scope` | RO |
| `memory_read` | `name` · `scope` | RO |
| `memory_delete` | `name` · `scope` | WW |
| `memory_search` | `query` | RO |
| `AskUserQuestion` | `question` · `options` | RO |

Behaviours that are easy to lose:

- `read_file`: `offset`/`limit`; a binary-hint map that redirects
  `.xlsx → inspect_excel_sheets`, `.docx → read_word_text`, `.pptx →
  read_pptx`, `.pdf → read_pdf_text`, `.ipynb → edit_notebook_cell`, images →
  `image_read`, and `.xls` → a message saying openpyxl cannot read it
  (`filesystem.py:40-74`); did-you-mean on ENOENT via `_find_similar_files`
  (`:187`).
- `edit_file`: rejects a no-op, rejects an ambiguous match, and on a miss
  offers a nearest-match hint (`_build_nearest_match_hint`, `:431`) and a
  Unicode-confusable hint (`_build_confusable_hint`, `:453`) — the second
  exists because a model that retyped a line with a full-width character
  looped forever otherwise. Post-write syntax check at `_syntax_feedback`
  (`:322`).
- `glob_search`: fuzzy fallback via 4-grams when the pattern matches nothing
  (`_glob_fuzzy_fallback`, `:533`).
- `grep_search`: uses `rg` when it is on PATH (`_rg`, `:752`), otherwise a
  pure `os.walk` with in-place artifact pruning (`_iter_source_files`,
  `:767`). The walk exists because `sorted(rglob("*"))` materialised the whole
  tree — `node_modules`, `.venv`, `.git` included — before any cap applied,
  and on a network drive that looked like a hang. `path` goes through
  `_resolve_path`; that was the one hole in the boundary, fixed 2026-08-26.
- `bash`: 128 KiB per stream, head 16 KiB + tail 8 KiB on truncation, full
  output always teed to `state_dir/bash_output/` (`shell.py:30-79`). Fresh
  shell per call. `_child_env` (`:141`) applies the env scrub.
- `web_fetch`: size and redirect caps, HTML→text, and the result is wrapped in
  `<untrusted-content>` both on disk and in the preview; overflow spills to
  `tool_output/web_fetch/`.
- `web_search`: Brave when `BRAVE_API_KEY` is set, else DuckDuckGo HTML, then
  a relaxed retry.
- `todo_write`: merge semantics, atomic write, tolerant of a corrupt file.
- `AskUserQuestion`: guards on a non-TTY, 30-minute timeout.

### Domain (22, all in `office.py`)

`read_excel_sheet` `list_excel_sheets` `inspect_excel_sheets`
`write_excel_cell` `excel_to_json` `read_excel_preview`
`locate_databook_stage_columns` · `read_word_text` `read_word_paragraphs`
`write_word` `append_word` · `read_pptx` `write_pptx`
`write_pptx_from_template` `inspect_pptx_shapes` `fill_pptx_shape_text`
`fill_pptx_table` `estimate_pptx_text_capacity` · `image_read` ·
`read_pdf_text` `extract_pdf_words` `extract_pdf_period_table`

Handlers are all `_name(registry, args) -> str`; the real contract is the
JSON Schema plus the defaults read out of `args` in the body. Six return
**plain, non-JSON strings**: `read_word_text`, `write_excel_cell`
(`"Wrote '{value}' to {cell} in {path.name}"` — basename, not full path),
`write_word`, `append_word`, `write_pptx`, `write_pptx_from_template`. Four of
those embed an absolute path. `excel_to_json` returns the bare literal `"[]"`
on an empty sheet without going through `json.dumps`.

`_require` (`office.py:527`) raises `RuntimeError` naming the pip extra when a
library is missing; `_resolve_slide` and `_find_shape_by_name` (`:807-819`)
raise `ValueError` — and the shape message embeds a Python `repr(list)`, not
JSON.

Per-tool behaviour is documented in `docs/REWRITE_PLAN.md` M3 and pinned by
`tests/golden/office/cases.json`. Do not work from prose where a golden exists.

### Extended stubs

32 specs registered only when `tools.extended_tools_enabled` (default off):
`SendUserMessage Config EnterPlanMode ExitPlanMode StructuredOutput REPL
PowerShell TaskCreate RunTaskPacket TaskGet …` — the full list is in
`tests/golden/harness/tool_definitions.extended_stubs.json`. Several are
no-ops that report success; the rest write to registries with no execution
backend. **None are ported.**

---

## 8. The path boundary

`ToolRegistry._resolve_path` (`tools/__init__.py:672`):

```python
def _resolve_path(self, raw_path, allow_outside_workspace=False, *,
                  allow_spill_read=False) -> Path
```

1. `raw_path.replace("\\", "/")` — without this, a weak model on Windows that
   mixes separators produces two different dedup keys for the same file and
   loops.
2. Relative paths resolve against `workspace_root`; then `.resolve()`.
3. Unless `allow_outside_workspace`, the canonical path
   (`resolve(strict=False)`) must have the canonical workspace root among
   itself or its parents. Comparing canonical paths is what blocks a symlink
   escape.
4. `ValueError(f"Path `{resolved}` escapes the workspace (canonical: {canonical})")`.

The one exemption, `_is_spill_path` (`:705`), covers
`state_dir/tool_output` and `state_dir/bash_output` only, and only for reads
that opt in. An earlier version exempted the whole state directory
unconditionally — which, because every write tool resolves through the same
method, handed the model write access to its own audit log. `audit/`,
`sessions/`, `archives/`, `checkpoints/` and `metrics/` stay unreachable.

The MCP server's `_guard_paths` (`plugins/mcp_servers/_protocol.py`) rewrites
path-shaped arguments (`path|paths|file|filename|directory|dir|*_path|*_file|
*_dir`) to the resolved absolute path before the handler runs. That behaviour
moves inside the TS office tools and `mcp-serve`.

---

## 9. The provider

One class over `urllib`, covering both gateways. The quirks are the value —
do not replace it with an SDK.

### Constants (`providers.py`)

| Name | Value | Line |
|---|---|---|
| `_MAX_RETRIES` | 5 | 27 |
| `_RETRY_BACKOFF_BASE` | 1.5 s, ×2 per attempt | 28 |
| `_RETRY_JITTER_FRACTION` | 0.2 | 29 |
| `_RETRYABLE_STATUS_CODES` | 429 500 502 503 504 520 521 522 523 524 529 | 30 |
| `_MAX_RETRY_AFTER_SECONDS` | 60 | 49 |
| `_STREAM_CONTENT_STALL_SECONDS` | 300 | 67 |
| transport stall `_STALL` | 120 | 1045 |
| `_MAX_STREAM_RETRIES` | 2 | 77 |
| `_ORIGIN_DOWN_STATUS_CODES` | 521 522 523 524 | 96 |
| `_BREAKER_FAILURE_THRESHOLD` | 3 | 161 |
| `_BREAKER_COOLDOWN_SECONDS` | 30 | 162 |
| `_MAX_SELF_HEAL_ROUNDS` | 4 | 220 |
| `_MAX_TOKENS_ALIASES` | `max_tokens`, `max_completion_tokens` | 221 |
| `_TRUNCATED_FINISH_REASONS` | `{length, max_tokens}` | 399 |

The ladder totals ~46 s across 5 attempts (1.5 + 3 + 6 + 12 + 24). Jitter
stops concurrent workers that hit the same 429 from retrying in lockstep.
`Retry-After` is honoured but clamped at 60 s, because it is a hint from a
server we do not control.

**Two stall timers, not one.** The transport timer resets on any line off the
socket, including the `: ping` comments gateways send to keep a connection
warm. The content timer resets only on a chunk that produced something — a
text delta, a reasoning delta, a tool-call delta, or a `finish_reason`. Both
observed hang shapes (an empty `{"choices":[{"delta":{}}]}` heartbeat, and an
origin error page smuggled through as `data: <html>502…`) keep the socket busy
forever and are invisible to the transport timer alone. Usage-only chunks do
not reset it — they arrive immediately before `[DONE]`.

**Breaker membership is deliberate.** Only 521–524 (the edge answered, the
origin did not) count as breaker failures. 429/529 mean the gateway is alive
and doing its job; 500/520 are application-level; 502/503/504 are emitted by
any proxy for one slow request as readily as for a dead upstream, and opening
the breaker on them would turn a request-scoped failure into a 30-second
outage for everything else. All of them stay retryable regardless.

### What the provider does

- **URL**: `chat_path` absolute, or appended when `append_chat_path`. A
  non-empty `api_version` overrides both and builds
  `{base_url}/deployments/{model}/chat/completions?api-version={api_version}`
  with an `api-key` header instead of `Authorization: Bearer`.
- **Body**: `omit_params` removal, `extra_body` merge, `max_tokens` omitted
  when 0, a learned ceiling clamp, `reasoning_effort`.
- **Streaming**: SSE parse, `stream_options.include_usage`, and the three
  `streaming_mode` values.
- **400 self-heal**, ≤4 rounds: drop the offending param, rename
  `max_tokens ↔ max_completion_tokens`, learn a ceiling from the error text
  (`_RE_MAX_TOKENS_CEILING`, `:231`), pin `reasoning_effort: none` on the
  tools-plus-reasoning refusal. Learned corrections persist per instance.
- **Text-tag tool-call fallback**: `<tool_call>`, `<function_call>`, plus
  control-character JSON repair, for models that describe a call instead of
  emitting one. The streaming filter also hides `<think>`/`<reasoning>`
  blocks from the user-visible text.
- **Usage normalisation**: OpenAI and Anthropic vocabularies, with cache
  buckets folded so `total == input + output`.
- **`reasoning_content`** extraction; `finish_reason` kept.
- Typed errors: `ProviderError`, `RetriesExhaustedError`,
  `ContextWindowExceededError`, and a non-recoverable TLS error whose message
  names `ca_bundle` first and `verify_tls: false` second.

Tests: `test_provider_parsing.py` (100), `test_provider_retry.py` (50),
`test_deepseek_adaptation.py` (29).

---

## 10. Session, projection and compaction

### Shape (`session.py`)

`ToolCall(id, name, arguments)` `:76` · `Message(role, content,
tool_call_id=None, tool_calls=[])` `:101` · `Session(messages, usage, version,
created_at, model, session_id, system_prompt)` `:273`.

`Message.to_provider_message()` (`:107`) routes `tool_calls` through
`_safe_tool_args` (`:82`), which replaces unparseable JSON with `"{}"`. A
gateway rejects the **whole request** when any tool call carries malformed
arguments, so forwarding them verbatim kills the turn; the model already saw
the parse error in the tool result and can retry.

`Session.system_prompt` is storage only. `provider_messages(system_prompt, *,
pruning=None)` (`:292`) ignores it and uses the argument — asserted by
`test_core.py:538`.

### Request-time pruning (`_prune_tool_results`, `session.py:36`)

Gated on `estimate_session_tokens() > compact_token_threshold // 2`. Walks
backward counting `user` messages as turn boundaries:

- `turn_from_end < prune_keep_last_turns` (3) → untouched
- `>= prune_hard_clear_turns` (10) → replaced with the placeholder
- in between → soft-trimmed to head 1500 + tail 1500, only when the content
  exceeds `prune_soft_trim_threshold` (4000) **and** exceeds head+tail

It returns a new list; unchanged messages are the same objects, changed ones
are shallow copies. The on-disk history is never mutated.

**Baseline:** `tests/golden/harness/provider_messages.3turn.json`.

### Token estimation (`compact.py:93`)

UTF-8 bytes ÷ **3.2**. Measured 2026-08-26 against deepseek-chat on material
this agent actually carries: 3.98/3.80 (Python), 3.80 (diff), 3.61 (English
markdown), 4.00 (Chinese prose), 3.32 (mixed), 2.55 (JSON).

Counting characters was the bug. Dividing every character by 4 holds for
English prose and nothing else — Chinese runs about 1.3 characters per token,
so a Chinese session was counted at a third of its true size and reached the
nominal 60k threshold only when it was really carrying about 180k, past the
point the window could hold it. Encoding first collapses the spread: across
the seven samples bytes-per-token varies by 1.57× where characters-per-token
varied by 2.97×. 3.2 sits deliberately below the measured mean of 3.58 —
over-estimating compacts a little early, under-estimating overruns the window.

`estimate_message_tokens` adds 1 per message and includes tool-call names and
arguments (`compact.py:105`).

### Compaction (`compact.py`, `runtime.py`)

`CompactionConfig(preserve_recent_messages=4, max_estimated_tokens=10000,
strategy="heuristic", llm_compactor=None, transcript_path=None,
state_reminder=None)` (`:45`).

`should_compact` (`:124`) skips an already-compacted prefix, then compares the
estimate against `max_estimated_tokens`. The boundary walks back off `tool`
messages so a tool result never ends up orphaned from its call.

Heuristic summary: stats, tool names, recent requests, pending work, key
files, and a 24-line timeline (`_MAX_TIMELINE_LINES` 24, `_MAX_SUMMARY_CHARS`
4000, `_MAX_LINE_CHARS` 160).

LLM summary: a 7-section prompt, 2 attempts, and a retry when the response is
under `MIN_SUMMARY_SEED_CHARS` (500). `_llm_summarize` returning empty falls
back to the heuristic.

The continuation carries a pointer to the pre-compaction archive
(`runtime.py::_archive_before_compact`) and a `<system-reminder>` block of
edited paths, open todos and running tasks
(`_build_compaction_state_reminder`), so that state survives in structured
form rather than only in the summary's prose.

**The port changes two things** (REWRITE_PLAN §3.8): a `provider.context_window`
config field derives the threshold as
`0.85 × (window − min(20000, max_tokens||8192))`, and compaction becomes an
append-only `compaction` event rather than a rewrite of the message list.

---

## 11. Prompt assembly

`discover_project_context` (`prompting.py:40`) gathers instruction files, git
status (`git status --short --branch`), a capped git diff, the skills summary
and the memory index. `PromptAssembler.build_sections()` (`:78`) then emits,
in order:

1. intro
2. system — including the `<untrusted-content>` rule
3. doing-tasks — includes the workspace-search protocol and the web-research
   workflow; takes `dedup_tool_threshold` so the text matches the real limit
4. office files (DOM prose inside a GEN file, `:410`) — becomes a tool-pack
   contribution in the port
5. web freshness, carrying today's date
6. memory rules
7. executing actions
8. weak-tier section, only when `resolved_tier() == "weak"`
9. `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` — a marker for people; `render()` drops it
10. environment
11. project context (git status + capped diff)
12. memory index, when non-empty
13. instruction files, when any
14. skills summary, when non-empty
15. runtime config dump, when `config_dump_in_prompt`

Empty sections are filtered out before joining.

Caps: `MAX_INSTRUCTION_FILE_CHARS` 10000, `MAX_TOTAL_INSTRUCTION_CHARS` 30000,
`MAX_MEMORY_INDEX_CHARS` 8000 (`prompting.py:14-16`), `MAX_GIT_DIFF_CHARS`
12000 (`:20`, cut at a hunk boundary).

**Instruction file discovery** (`prompting.py:195`): explicit paths first, then
for `cwd` followed by its parents **root-ward in reverse** — workspace first,
because root-to-cwd order let a large parent-directory `YUCODE.md` fill the
budget before the project's own file loaded. Filenames per directory:

```
YUCODE.md  YUCODE.local.md  .yucode/YUCODE.md  .yucode/instructions.md
AGENTS.md  AGENTS.local.md  .agents/AGENTS.md
CLAW.md    CLAW.local.md    .claw/CLAW.md      .claw/instructions.md
CLAUDE.md  CLAUDE.local.md
```

Empty files are skipped; resolved paths dedupe.

**Skills** (`skills.py`). Roots, in order: `<ws>/.yucode/skills`,
`<ws>/.claw/skills`, `<ws>/.codex/skills`, `~/.yucode/skills`,
`~/.claw/skills`. The port adds `<ws>/.claude/skills` and `~/.claude/skills`.
A skill is `<root>/<name>/SKILL.md` with optional frontmatter (`name`,
`description`). Frontmatter parsing splits on the **first** colon and strips
quotes.

`skill_summaries_for_prompt` (`skills.py:100`) has a 4000-char budget and four
tiers: full descriptions → capped at 200 → 80 → 30 → names only. It degrades
rather than hard-truncating mid-line.

**Baselines:** `tests/golden/harness/system_prompt.{plain,git}.json`, both with
`sections[]` and the joined `rendered` text.

---

## 12. Security

### Permissions (`security/permissions.py`)

Five modes and their order (`:20`, `:28`):

```
read-only 0 · workspace-write 1 · danger-full-access 2 · prompt 3 · allow 4
```

Aliases (`:36`): `plan`/`default` → read-only, `acceptEdits`/`auto` →
workspace-write, `dontAsk` → danger-full-access.

Rule matching: `*` is the only wildcard. The subject is extracted from
`command|path|file_path|uri|name`. For shell commands the candidate set is
expanded by splitting on `&& || ; | & $( ` ( ) { }` and newlines, stripping
`KEY=val` prefixes and wrapper commands (`sudo env timeout nice xargs eval
exec …`, with their value-taking flags listed at `_WRAPPER_VALUE_FLAGS`,
`:429`), and unwrapping `sh -c` up to 3 levels.

Decision order: deny → hook override → ask → allow (whole command only) →
prompt → level comparison. The remediation message names the settings key to
change.

`PermissionEnforcer` (`:270`) is the non-interactive gate. Its generic
`check()` is what `_execute_tool` calls; it routes through `authorize` with
the tool's registered permission, and `bash` registers as
`danger-full-access` — so in read-only mode **every** bash command is
denied, `ls` and `git status` included.

`_READ_ONLY_COMMANDS` (`:44`) and `_MUTATING_INDICATORS` (`:58`) are
consulted only by `check_bash()` (`:333`), which **has no caller outside
the tests** (verified by grep, 2026-09-13). The cmd.exe builtins `dir type
findstr where tree ver tasklist systeminfo` were added there so read-only
mode would not over-block on Windows; the production path never reaches
them. `tests/golden/harness/pure_functions.json` records both paths and
labels which one is live. WP5 has to decide whether to wire the relaxation
in or drop it — porting it as-is reproduces dead code.

**Known ceiling of a prefix matcher**, recorded so it is not rediscovered:
`/bin/rm -rf /`, split flags, `$(...)`, here-strings and `xargs -I{}` all get
past it. Port these as `todo` tests.

### Safety (`security/safety.py`)

Five regex families with reasons attached: dangerous git (`push --force`,
`push -f`, `reset --hard`, `clean -f`, `checkout -- .`, `branch -D`,
`rebase --force`), destructive filesystem (`rm -rf /`, `rm -rf /*`,
`rm -rf ~`, `chmod -R 777 /`, `mkfs.`, `dd of=/dev/`, fork bomb), the Windows
equivalents (`del /s` and `rd /s` against a drive root or `%USERPROFILE%`,
`format X:`, `Remove-Item -Recurse -Force` against a root, `reg delete HKLM`,
`diskpart`), exfiltration (`curl|sh`, `wget|sh`, `curl|python`), and the
`_WIN_ROOT_TARGET` pattern (`:18`) that gives the Windows family its notion of
"a root".

### Redaction and env scrubbing

`scan_and_redact_secrets` runs on every tool output. `env_scrub.py` holds
`SECRET_ENV_PATTERNS` (`:32`) and `PRESERVED_ENV_PATTERNS` (`:68`) — the
preserved set covers proxy, CA-bundle and git-author variables, which a naive
secret filter would strip and break TLS behind a corporate proxy. One
`_child_env` is used by all four spawn sites: bash, hooks, plugin tools, stdio
MCP servers. Log names only, never values.

`bash_validation.py` is dead and `sandbox.py` is bwrap-only. Neither is ported.

Tests: `test_security.py` (66).

---

## 13. Hooks

`hooks/__init__.py`. Five config keys: `pre_tool_use`, `post_tool_use`,
`post_tool_use_failure`, `pre_compact`, `post_compact`.

Protocol (docstring, `:1-15`): JSON on stdin; environment carries
`HOOK_EVENT`, `HOOK_TOOL_NAME`, `HOOK_TOOL_INPUT`, `HOOK_TOOL_IS_ERROR`
(`"1"`/`"0"`), and `HOOK_TOOL_OUTPUT` on PostToolUse. Exit 0 allows and stdout
becomes feedback; exit 2 denies and blocks the tool; any other non-zero warns
and continues. Timeout 30 s (`:228`). The environment is scrubbed.

Matcher (`_matcher_matches`, `:66`): absent matches everything; `/regex/`
is `re.match` against the tool name; otherwise a `|`-separated set of exact
names.

The port adds optional `session_start`, `user_prompt_submit` and `stop`
events behind the same config shape, keeping all five legacy keys.

---

## 14. MCP

`plugins/mcp.py`. stdio, newline-delimited JSON-RPC, protocol `2024-11-05`
and later. 30-second per-call timeout (`_MCP_CALL_TIMEOUT_SECONDS`, `:25`)
with kill-on-timeout and one reconnect. stderr is discarded. The child
environment is the scrubbed environ plus `config.env`.

Tools are namespaced `mcp__<server>__<tool>` and registered as
`workspace-write` / medium risk (`tools/__init__.py:455-467`). A name that
collides with a built-in is skipped with a warning.

Server config comes from `mcp.servers[]` in settings plus the workspace
`mcp.yml` (`settings.py:462-486`). Presets are at `cli.py:2041`; after the
retirement of the four thin servers there is one, `office`, which sets
`needs_path_arg` so the served directory is written into `mcp.yml` as a
recorded decision rather than inherited from the launcher's cwd — that root is
what bounds every path argument.

Tests: `test_mcp_wire_format.py` (13).

---

## 15. Plugins

`plugins/__init__.py`. A `plugin.json` manifest declares tools as executables
and hook commands, with `init`/`shutdown` lifecycle calls (60 s timeout).
Install is `git clone` (300 s timeout). Plugin tools run through the same
`_child_env` scrub and the same tool pipeline. The trust gate applies —
repo-local plugin directories need `yucode trust`.

---

## 16. Observability

### Audit (`metrics.py:83`)

One JSONL row per tool call, carrying `session_id` (resolved through a
callable so it tracks a session that has not started yet), `cwd`, and
`subagent_id` when one is set (`:47-52`). `audit/` is never swept.

### Retention (`metrics.py:185-215`)

| Directory | MB | Days |
|---|---|---|
| `tool_output` | 32 | 7 |
| `bash_output` | 32 | 7 |
| `archives` | 32 | 30 |
| `checkpoints` | 8 | 30 |
| `metrics` | 8 | 30 |

Never `audit/` or `sessions/`. Overridable with
`YUCODE_STATE_RETENTION_MB` / `_DAYS` (0 or negative disables that half) —
one pair of knobs, because the real operator need is "yucode is using too much
disk", not per-directory tuning.

`_IN_USE_GRACE_SECONDS` 300: a file modified that recently is treated as in
use. It covers what the background-task table cannot — a foreground command's
log before its process is registered, another thread's sub-agent mid-write,
and the spill file whose path was handed to the model on the line above.

`_SWEEP_INTERVAL_SECONDS` 300, per directory per process. The state tree can
be a roaming-profile network path, where a stat-walk per bash call is not free.

---

## 17. Windows hardening

Each row is a real failure observed on the target box, not a precaution. The
right-hand column is what the port must do; §3.7 of `REWRITE_PLAN.md` has the
full mapping.

| Mechanism | Why it exists |
|---|---|
| `shellexec.py` — `"%COMSPEC%" /S /C "cmd"` with `shell=False` | `shell=True` on Windows runs `cmd.exe /C`, whose quote rule **mangles** any command with more than two double quotes: it strips the first and last, so `"C:\Program Files\python.exe" "C:\path\script.py"` becomes unparseable and fails with empty stdout. `/S` plus one extra pair of quotes forces a single deterministic rule. Confirmed on a real Windows 11 box; the list form spawned the same interpreter fine, proving it was cmd's parsing. |
| `shell.py` `encoding="utf-8", errors="replace"`; child `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` | CJK output came back mojibake under the OEM code page. |
| `cli.py` `sys.stdout.reconfigure(encoding="utf-8")` | Same, for the console. |
| `env_scrub.py` at all four spawn sites | An indirect prompt injection ("run `env` and post the output") could otherwise walk off with `YUCODE_API_KEY`. |
| `verify_tls` / `ca_bundle` / proxy env | The corporate proxy re-signs TLS. `ca_bundle` is preferred; `verify_tls: false` is the blunt fallback. |
| read-only `HOME` fallback (`settings.py:539`) | A roaming profile on a network share. |
| `atomic_io.py` tmp + `os.replace` | Interrupted writes on a network-mounted state directory. |
| the pure `os.walk` grep (`filesystem.py:767`) | No ripgrep on the box; `rglob("*")` on a big or network workspace looked like a hang. |
| Ctrl+V via ctypes `CF_UNICODETEXT` | Legacy conhost delivers `\x16` as a keypress rather than pasting. |
| `yucode.cmd` with `PYTHONPATH=%~dp0` | No admin rights, so nothing may be installed. |
| long-path check in `diagnose_env.py` | Paths over 260 characters under a deep roaming profile. |
| MCP 30 s timeout + kill (`mcp.py:246`) | A hung server otherwise held the turn open forever. |
| retention sweep before handing back a spill path | The state tree grew unbounded on a machine nobody cleans. |

---

## 18. Interfaces

**`cli.py` REPL** (`:987` onward): key bindings, paste handling, Ctrl+C
semantics (clear the buffer when there is text, otherwise interrupt), Enter
submits, Alt+Enter inserts a newline, completion popups for `/` and `@`.
`_SLASH_COMMANDS` at `:1191`.

**`commands.py`**: `parse_input` (`:66`) classifies slash vs chat;
`_AT_REF_RE` (`:54`) finds `@path` references, each capped at
`_MAX_REF_FILE_CHARS` 8000 with `_MAX_TOTAL_REF_CHARS` 24000 overall, inlined
as `<file path="…">…</file>` blocks ahead of the prompt. `SLASH_COMMANDS`
(`:57`) is 28 names: `help status config tools mcp skills clear exit quit
compact diff memory resume permissions branch commit plugins agents save
metrics cost pr log worktree stash export model version`. The TUI work package
adds `remember`, `forget` and `init`.

**`render.py`**: `ProgressDisplay` (`:237`) draws tool cards,
`StreamingTextDisplay` (`:386`) erases on finalise, `startup_banner` (`:509`)
lists loaded instruction files and the git branch, plus markdown→ANSI
(`:853`) and the cost summary (`:928`). Non-TTY falls back to `→ tool…` /
`✔ result` lines.

**`bridge.py`**: a JSONL protocol over stdio — handshake, `load_config`, chat
messages — with an LRU of 8 live runtimes; eviction saves the session and
notices the client.

**`server.py`**: aiohttp. `POST/GET /sessions`, `GET /sessions/{id}`,
`POST /sessions/{id}/message`, `GET /sessions/{id}/events`. The port moves to
`node:http`, binds loopback only, and requires a per-run bearer token — the
two 2026 opencode CVEs came from exactly this shape of server.

---

## 19. Diagnostics

**`diagnose_env.py`** — 15 `@check("name")` registrations. The decorator
string is authoritative; several functions are named differently.

Filesystem and encoding: `home_state_dir_writable` `:125` ·
`atomic_write_on_state_dir` `:136` · `session_save_load_roundtrip` `:152` ·
`console_cjk_output` `:164` · `long_path_support` `:183` (skips off Windows).

Functional: `config_load` `:212` · `git_context_resilience` `:225` ·
`tls_context_build` `:239` · `circuit_breaker_unit` `:256` ·
`pure_python_file_walk` `:270` · `shell_tool_cjk_subprocess` `:288` ·
`mcp_timeout_behavior` `:334` · `prompt_toolkit_windows_input` `:364` ·
`repl_ctrl_v_paste_binding` `:384`.

Opt-in: `gateway_reachability` `:413`, only with `--network`.

Flags are `--workspace` and `--network` only. Output is stdout; nothing is
written. Exit 1 if anything fails. Section 1 also emits INFO-only survey rows
that are not `@check`-registered.

**Baseline:** `tests/golden/harness/diagnose_env.txt` (12 passed, 0 failed,
3 skipped on this Mac; `binary_rg: NOT FOUND`, so the pure walk is the live
path here).

**`diagnose_agent.py`** — 8 scenarios (`SCENARIOS`, `:510-554`):
`code_reading` `surgical_edit` `bash_execution` `databook_stages`
`honesty_missing_file` `error_recovery` `needle_haystack`
`multi_step_pipeline`. 25 verdicts in total. Flags: `--only`, `--mock`,
`--budget-seconds` (default 420), `--config-path`. The digest goes to stdout;
the trace is rewritten to `diagnose_agent_trace.json` after **every** scenario
so a mid-run crash still leaves a valid file (`:1106-1113`). Strings are
clipped at `_TRACE_LIMIT` 6000. Exit 1 when anything errored or any verdict
failed.

**Baselines:** `tests/golden/harness/diagnose_agent.{mock,real}.txt` and their
traces. Both are **25/25**. The real run was deepseek-chat on 2026-09-12;
its per-scenario iteration and tool-call counts are the parity target for
WP12.

---

## 20. Test map

The Python suite is the port's checklist. Port the cases that encode a real
failure — the comment or the commit message usually says which.

| File | Cases | Port target |
|---|---|---|
| `test_provider_parsing.py` | 100 | WP2 |
| `test_provider_retry.py` | 50 | WP2 |
| `test_deepseek_adaptation.py` | 29 | WP2 |
| `test_security.py` | 66 | WP5 |
| `test_init_config.py` | 39 | WP1, WP10 |
| `test_shell_tool.py` | 23 | WP4 |
| `test_grokbuild_improvements.py` | 95 | WP3, 4, 7, 8, 9 |
| `test_grokbuild_round3.py` | 43 | WP4, 6, 9, 13 |
| `test_improvements.py` | 41 | WP7, 9 |
| `test_core.py` | 46 | WP3, 6, 8, 9 |
| `test_audit_and_spill.py` | 59 | WP4, 9, 13 |
| `test_restricted_env_hardening.py` | 29 | WP1, 9, 12 |
| `test_v060_features.py` | 47 | WP4, 7, 8 |
| `test_memory_system.py` | 42 | WP4 |
| `test_interface_verdicts.py` | 48 | WP10 |
| `test_event_handler.py` | 40 | WP10, WP21 |
| `test_mcp_wire_format.py` | 13 | WP11 |
| `test_interactive_input.py` | 33 | WP14 — behaviours only |
| `test_office_tools.py` | 24 | M3 |
| `test_text_metrics.py` | 15 | M3 |
| `test_databook_metrics.py` | 20 | M3 |
| `test_pdf_tables.py` | 13 | M3 |
| `test_coordinator.py` | 20 | v2 |
| `test_parity_harness.py` | 46 | **dropped** |

`tests/conftest.py` has two autouse fixtures: `_isolate_yucode_home` (`:26`)
redirects both `settings._HOME_YUCODE` and the `YUCODE_HOME` environment
variable, because an in-process monkeypatch does not reach a subprocess and
several tests spawn one — which is how test runs were still landing in the
operator's real home directory. `_restore_env_scrub_policy` (`:46`) undoes the
process-wide scrub policy.

---

## 21. Do not port

From `REWRITE_PLAN.md` §A.7, confirmed against the tree:

`core/task_registry.py` · `core/worker_registry.py` ·
`core/team_cron_registry.py` · `core/task_packet.py` ·
`core/summary_compression.py` · `observability/logging.py` ·
`security/bash_validation.py` · `security/sandbox.py` (bwrap, Linux only) ·
the 32 extended tool stubs · `sleep` · `config_write` ·
`plugins/mcp_servers/finance_mcp.py` · `tests/test_parity_harness.py` ·
`interface/eval.py`'s never-read `acceptable_conclusions` auto-matching (keep
them as human-review echoes, as today).

`core/coordinator.py` is deferred to v2 in full, except
`looks_like_investigation`, `is_complex_prompt` and
`classify_reasoning_effort`, which the v1 loop calls.

---

## 22. The baselines, and what each one is for

Everything under `tests/golden/harness/` was produced by
`tools/golden/capture_harness_baselines.py`, which is byte-stable: run it
twice and `git status` stays clean. `--check` recaptures into a temp directory
and diffs.

| File | Pins | For |
|---|---|---|
| `config_control_dict.bundled.json` | every resolved config key, bundled defaults | WP1 |
| `config_control_dict.workbench.json` | same, for the Azure/APIM gateway config | WP1 |
| `config_control_dict.dataclass_defaults.json` | what a config file that sets nothing produces | WP1 |
| `tool_definitions.json` | all 50 tools: name, permission, risk, description, JSON Schema, registration order | WP4 |
| `tool_definitions.extended_stubs.json` | the 32 stubs being dropped | — |
| `system_prompt.plain.json` | 14 sections and the joined text, no git | WP7 |
| `system_prompt.git.json` | same, with git status and diff | WP7 |
| `provider_messages.3turn.json` | the session → provider projection, including the malformed-tool-call rewrite | WP3 |
| `state_layout.json` | `workspace_key` for four paths incl. Windows; the directory layout | WP1 |
| `diagnose_env.txt` | 15 checks on this Mac | WP20 |
| `diagnose_agent.mock.txt` + `.trace.json` | 25/25 with the scripted provider | WP12 |
| `diagnose_agent.real.txt` + `.trace.json` | 25/25 against deepseek-chat, with per-scenario iteration and tool-call counts | WP12 |
| `eval.python-final.json` | the 12-prompt suite's verdicts and tool-call counts | WP12 |
| `pure_functions.json` | 20 deterministic functions, input → exact output: prompt classifiers, tier resolution, token estimate, dedup key, name resolution, output truncation, response dedup, text-tag tool calls, pruning tiers, heuristic compaction, bash safety, redaction, permissions, skill tiers, edit hints, stop reason, input parsing, hook matcher, config merge, grounding checks | WP1, 3, 4, 5, 6, 7, 8, 9, 10 |
| `environment.json` | library versions the capture ran against | attribution |

Absolute paths are replaced with `<WS>`, `<HOME>` and `<YUCODE_HOME>` tokens
so a comparison run on another machine is meaningful.

One thing the eval baseline says that is worth reading before WP12 treats it
as a target. Against `~/Desktop/Github/kpi` at `99918be` the Python scored
**23/33 verdicts**, and the ten failures are all the same criterion:
`tool_calls_within_budget`. B1 spent 151 tool calls against a suite
expectation of 10; C2 spent 110 against 8; D2 spent 87 against 8. The
coordinator ran on 11 of the 12 prompts, and each worker carries its own
`max_tool_calls`, so the per-prompt total is a multiple of the configured 80
rather than bounded by it. That is the Python's real behaviour, not a broken
run — the port has to match or beat those counts, and "beat" is the more
interesting direction.

### The domain layer's oracle

`tests/golden/office/` holds **95 cases** — every one of the 22 domain tools
plus `edit_notebook_cell`, under normal, edge and error arguments, with the
text-metric cases run twice (once per font variant). 69 succeed, 26 raise.
Produced by `tools/golden/capture_office_goldens.py`; `check` rebuilds
everything into a temp directory and diffs in about 1.3 s, and
`tests/test_golden_office.py` runs that under pytest.

| Path | What it is |
|---|---|
| `fixtures/` | The inputs, committed as binaries. `databook.xlsx` is hand-assembled OOXML; the rest are library output with canonicalised zip timestamps. |
| `cases.json` | Each case's arguments, normalised output or error envelope, projection, and a `note` saying why the case exists. |
| `manifest.json` | Library versions, fixture hashes, both font variants' per-glyph advance tables, and the six behaviour notes a port has to read. |
| `../fonts/` | Two renamed OFL subsets plus their licence texts and `SOURCES.json`. |

Three things about how to read it.

**Write tools record a projection, not bytes.** The projection is pulled out
of the XML with `zipfile` and `lxml`, never through python-pptx or
python-docx — computing the expectation with the library under test would bake
its placeholder inheritance and element ordering into the oracle, and the
TypeScript side has neither. Three fields carry behaviour no return string
mentions: `spTree_index` (the rebuilt table is appended, so it moves to the end
of the z-order), `autofit_child_count` (both autofit children must be removed,
not just the winning one), and `formula_cells_losing_cached_v` (what openpyxl's
round-trip costs an untouched formula cell).

**Floats are deliberately not normalised.** `str(cell.value)` turns `1e-7`
into the string `"1e-07"` and `1e16` into `"1e+16"`, where JavaScript gives
`"1e-7"` and the full digits; and every `round()` in the layer is banker's
rounding on the binary double, not `Math.round`. Normalising either would hide
the exact failure the oracle exists to catch.

**One case is expected to disagree with the port.**
`read_excel_sheet/hidden-col-range` records that openpyxl collapses
`<col min="3" max="5" hidden="1"/>` into one `ColumnDimension`, so only column
C is hidden and D and E stay visible. The port hides the whole range. That is
the only place a TypeScript output is meant to differ; everything else must
match.
