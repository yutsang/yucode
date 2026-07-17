# Agent upgrade notes — code-structure review for the GPT-5.5/Workbench era

**Audience:** the implementing agent (Claude Sonnet 5) doing follow-up work after `WORKBENCH_PLAN.md`.
**Context:** the primary model changes from Qwen3-32B (weak, local) to GPT-5.5 via the KPMG Workbench gateway (strong, reasoning, `reasoning_effort` control). This is a review of the whole runtime — tool surface, security, intelligence-tier system — with that lens. Every finding cites the code as of 2026-07-16; re-verify lines before editing.

**Priorities:** P0 = required for the company deployment (all P0s are already work items in `WORKBENCH_PLAN.md` — do those first). P1 = small, high-value, do together with the deployment. P2 = only after on-site smoke passes; measure before building.

---

## 1. Provider layer (`coding_agent/core/providers.py`) — solid, two P0 gaps

Current state: single zero-dependency urllib client; retries 429/5xx with backoff (`:23-27`), streaming reader on a daemon thread with 120 s stall detection (`:335-493`), hybrid empty-response fallback (`:154-171`), `<tool_call>`-in-text fallback parser for models that don't use native function calling (`:646`). This design is the deployment advantage on a no-admin box — **keep it; do not switch to the openai SDK.**

- **P0** — `temperature` unconditionally sent (`:185`); GPT-5-class rejects it → plan WI-1 (`omit_params` + 400 self-heal).
- **P0** — no Azure-style URL/auth → plan WI-2 (`api_version` field).
- **P1** — after WI-1, make sure the self-heal's learned omissions are stored per provider instance (not per call) so only the first request pays a retry round-trip.
- **P2** — `reasoning_effort` as a first-class `ProviderConfig` field plus a per-call override argument on `complete()` — prerequisite for WI-7 (per-phase effort). Until then `extra_body` is fine.

## 2. Intelligence-tier system — keep the shape, extend the axis

Current state: binary strong/weak resolved from model-name hints (`config/settings.py:23-43`); `gpt-5*` → strong. Exactly **5 gated call sites**, all `resolved_tier() == "weak"`: coordinator (investigation forcing), runtime (grounding retry gate), prompting (`_weak_tier_section`). Clean gating — under GPT-5.5 all weak paths are dormant automatically.

- **Do NOT delete weak-tier code.** It costs nothing when dormant and a local-model fallback may return.
- **P2 (WI-7 design sketch)** — the real upgrade axis is no longer strong/weak but *how much reasoning to buy per call*:
  1. `ProviderConfig.reasoning_effort: str = ""` (empty = don't send).
  2. `OpenAICompatibleProvider.complete(..., body_overrides: dict | None = None)` merged after `extra_body`.
  3. Coordinator phases pass overrides: planner/validator `high` is *not* obviously right — planning needs JSON reliability more than depth. Suggested starting point: planner `medium`, workers `medium`, validator `low`, synthesis/compaction `low`, and let the user's global setting win when set. Mirror of the per-agent overrides in the pptx reference (`python-pptx/fdd_utils/config.example.yml`, `agents.2_Auditor.reasoning_effort: low`).
  4. Measure with the eval runner (§7) before and after; skip the whole item if global `medium` is good enough.
- Note: `resolve_intelligence_tier` treats unknown models as strong — correct for gpt-5; no change needed.

## 3. Tool surface — complete; tune the exposure, not the tools

Inventory (~40 tools): filesystem (`read_file`, `read_files`, `edit_file`, `write_file`, `glob_search`, `grep_search`, `list_directory`, `file_outline`), `bash`, web (`web_fetch`, `web_search`), office (`read_excel_sheet`, `list_excel_sheets`, `inspect_excel_sheets`, `excel_to_json`, `read_excel_preview`, `write_excel_cell`, word/pptx/pdf readers+writers, `image_read`), notebook, memory (5 tools), `agent`, misc (`todo_write`, `load_skill`, `structured_output`, `config_read/write`, mcp resources, `sleep`, `tool_search`).

- **P1** — company config should trim exposure via `tools.disabled`: `web_search`/`web_fetch` if corporate egress is blocked (probe on-site — a hanging web tool wastes a whole agent turn), and `edit_notebook_cell`/mcp tools if unused. Every request ships every enabled tool schema to the gateway; trimming cuts tokens, latency, and confusion. Put the recommended `tools.disabled` list into the §6 template of the plan after on-site probing.
- **P1** — verify office tools fail with a *clear* "install openpyxl" message when optional deps are missing (the FDD skill depends on this behaviour to fall back to the .txt path). If the error is an ugly traceback, wrap it.
- **P2** — tool descriptions were tuned for weak models (verbose, defensive). GPT-5.5 doesn't need the hand-holding, but shortening them is low-yield churn; leave unless token budget becomes a real problem.

## 4. Security posture — good bones, Unix-shaped; three Windows gaps

Current state: permission modes `read-only < workspace-write < danger-full-access` plus `prompt`/`allow` (`security/permissions.py:19-33`); bash gating in `check_bash` (`:300-322`); pattern-level blocker `check_bash_safety` (`security/safety.py`) with dangerous-git + dangerous-fs patterns; `bash_validation.py` (sed-in-place, path traversal, command classification); Linux bwrap sandbox (`security/sandbox.py`, wired in `tools/shell.py:60`); `audit.enabled: true` in bundled config.

Findings, in decreasing severity:

- **P1 — Windows catastrophic patterns missing.** `_DANGEROUS_FS_PATTERNS` (`safety.py:26-31`) covers `rm -rf /`, `mkfs` — Unix only. On the company PC bash runs through `cmd.exe` (`shell.py:94-97`, `shell=True`), where the equivalents are unguarded. Add accident-prevention parity: `del /s /q` + `rd /s /q` / `rmdir /s /q` targeting a drive root or `%USERPROFILE%`, `format <X>:`, `Remove-Item -Recurse -Force` on a drive root or home, `reg delete HKLM`, `diskpart`. Keep the same "catastrophic only" philosophy — this is not a malware filter, it stops the model from fat-fingering the disk.
- **P1 — `_READ_ONLY_COMMANDS` is Unix-only** (`permissions.py:43-52`). In `read-only` mode on Windows, `dir`, `type`, `findstr`, `where`, `tree`, `ver`, `tasklist`, `systeminfo` are all blocked (over-blocking = safe but annoying and burns agent iterations on denials). Add the cmd builtins. PowerShell invocations (`powershell -Command ...`) stay non-read-only — conservative is fine there.
- **P1 — pick and document the company permission posture.** Facts: `workspace-write` allows **every** bash command (`permissions.py:322` fall-through — only the safety patterns stand between the model and the OS), there is **no OS sandbox on Windows** (bwrap is Linux-only), and `prompt` mode denies bash outright unless an interactive prompter confirms (one-shot mode has no prompter). Recommendation: interactive daily use → `workspace-write` (default) after the safety-pattern upgrade; first on-site days or demo on sensitive machines → `prompt` in interactive sessions. State this tradeoff in `README-WINDOWS.md` rather than inventing new mechanism.
- **P1 — verify the audit trail.** `audit.enabled: true` ships in the bundled config, but I did not verify what the audit module actually records. On a company machine, a local log of every bash command + file write is genuinely useful (self-protection). Check it logs: timestamp, tool name, command/path, returncode, and where the file lives. If bash commands aren't recorded, add that — small and high-value.
- **P2** — `check_bash_safety` and `bash_validation.py` parse bash syntax; cmd.exe chaining (`&`, `^` escapes) differs. `_MUTATING_INDICATORS` already catches `&&`, `>`, `;` which covers most cmd chaining; full cmd parsing is not worth it. Revisit only if on-site logs show misclassification.

## 5. Prompting & memory layer — no changes needed for the switch

`memory/prompting.py` already: states the platform (`:109` — so GPT-5.5 knows it's on Windows and emits cmd-compatible commands), discovers CLAUDE.md/AGENTS.md instruction files, injects skill summaries, weak-tier section dormant. Compaction (60 K threshold, heuristic + optional LLM compactor) is model-agnostic.

- **P2** — a "strong-tier" system-prompt trim (the prompt carries weak-model guidance like the bash quick-map at `:340`) — only if prompt-token cost at the gateway turns out to matter. Measure first via `usage` from non-stream responses.

## 6. Orchestration — `auto` (coordinator kept), retry rounds decoupled from `max_iterations`

Superseded: this section originally recommended `orchestration_mode: single` for the company default, on cost/predictability grounds. Reversed after real on-site use of the fdd-commentary skill (multi-account, multi-project, cross-checked commentary + PPTX export) showed genuine value from the work→validate loop — the task is exactly the kind of multi-step, verifiable-output work the coordinator is for. Bundled default is now `orchestration_mode: auto`.

The actual problem observed on-site wasn't the coordinator existing — it was that its validation-failure retry loop reused `runtime.max_iterations` (a single-agent-turn budget, default 32) as the number of full research→work→validate rounds to retry. Each round reruns an entire worker (itself up to `max_worker_steps` turns), so one rejected validation could redo the whole work phase up to 32 times — this is what actually caused a real run to spiral through dozens of retries and repeated "reached maximum iterations" messages. Fixed by adding a dedicated `runtime.max_coordinator_retries` (default 3), used in `orchestrate()`'s retry loop instead of `max_iterations`, which now only governs single-agent-turn and per-worker step budgets. See `tests/test_coordinator.py` for the regression proof (old code: 999 `max_iterations` → 999 retries instead of the intended 2).

- **P2** — the one coordinator feature worth porting to single mode: `_run_concrete_validators` (auto-pytest when `tests/` exists, `core/coordinator.py`). In single mode nothing verifies the model's "done" claim except the bash-returncode grounding check. A post-answer pytest hook in single mode would close that — but it changes behaviour for every user, so design it as opt-in config and do it only after on-site experience shows the need.
- Grounding framework (`_ToolObservations`, `_check_final_answer_grounding` in `core/runtime.py`) is tier-independent and cheap — keep as is; `claimed_success_but_bash_failed` applies to all tiers already.

## 7. Eval & the pending v0.6.0 release gate

- `interface/eval.py` + `tests/eval_prompts.yaml` were built for the "run on Qwen3-32B box" workflow. Repurpose unchanged: run the suite through Workbench on the company PC (`yucode eval`), bring the JSON home, diff against the Qwen3-32B baseline. This doubles as the GPT-5.5 acceptance test and the WI-7 before/after harness.
- **Decision for the owner:** v0.6.0 is tagged locally but unpushed, previously gated on a Qwen3-32B eval. With the full switch to Workbench, either (a) drop the Qwen gate and push after the workbench changes land as v0.7.0, or (b) still run the Qwen eval once for the record. Don't let the tag sit forever.

## 8. FDD skill (`.yucode/skills/fdd-commentary/SKILL.md`)

What ships: a skill distilling the python-pptx FDD pipeline (Generator + Auditor house rules — number format, BS/IS structure, banned patterns, length caps) into one agentic workflow: locate databook → parse Indicative-adjusted columns → draft → self-audit → write markdown. Discovery: workspace `.yucode/skills/` or `%USERPROFILE%\.yucode\skills\` (`memory/skills.py:104-113`); the agent loads it via `load_skill`.

- **Testing on the Mac:** point a scratch workspace at a databook export from `/Users/ytsang/Desktop/Github/python-pptx/` (e.g. `Project Gold Kunshan.databook.txt`). **Do NOT copy databook files into this repo — they are client data.** Test out-of-tree, gitignore any scratch dirs.
- **PPTX export — done (2026-07-16), superseding the note below.** The trial worked; the user asked for the full databook-to-report pipeline. Rather than reuse `write_pptx_from_template` (too generic — new-slides-only, no way to target an existing named shape) or shell out to the reference project's 6220-line `fdd_utils/pptx.py` (its font-metrics system turned out to be unused for actual font sizing — real size is hardcoded 9pt; the metrics only predict line counts for content allocation), built three new discovery-first tools in `tools/office.py`: `inspect_pptx_shapes` (reports any template's actual shape names/types/dimensions — no assumption of a fixed naming convention), `fill_pptx_shape_text` (name-matched text write), `fill_pptx_table` (always deletes+rebuilds the target shape as a fresh table sized to the data — mirrors the reference project's own behavior of never reusing a placeholder's table, and avoids a silent-cropping bug an earlier "fill in place if already a table" design had). Skill's new "PPTX export" section teaches the allocation/capacity-estimation workflow using simple chars-per-inch heuristics (no dedicated line-measurement tool) and explicitly punts overflow to the follow-ups list rather than auto-generating continuation slides (YAGNI). 14 new tests in `tests/test_office_tools.py`, all passing against a synthetic template built with real python-pptx.
- **Still open:** a Chinese-output variant (the reference `prompts.yml` has full Chi sections to distill); an HR-databook sibling skill (`python-pptx-hr` has the same shape); continuation-slide auto-generation if overflow turns out to be a frequent problem in practice.

## 9. Suggested execution order

1. ~~`WORKBENCH_PLAN.md` WI-1 → WI-6 (P0, in order; tests green after each).~~ **Done 2026-07-16.** All six implemented (omit_params + 400 self-heal, api_version/Azure URL, workbench bundled default, test_connection.py --azure probe, prompt_toolkit fallback, delivery bundle) with 496 passing tests.
2. ~~P1 batch~~ **Done 2026-07-16.** Windows dangerous-fs patterns + read-only cmd builtins (§4), audit trail now records every bash/write_file/edit_file call (§4), office-tool errors confirmed clean + regression-tested (§3), permission posture paragraph shipped in `yucode-deliver/README-WINDOWS.md` §6.
3. **Bonus finds surfaced by verification, also fixed:** `_apply_cli_overrides` was manually reconstructing `ProviderConfig` field-by-field and would have silently dropped `omit_params`/`api_version` on any `--model` CLI override — replaced with `dataclasses.replace()`. `pyproject.toml` was missing a `tomli` dependency for Python 3.10 (`coding_agent/__init__.py` needs it) — invisible in dev environments because other tooling pulls it in transitively, but fatal on a genuinely clean install (i.e. the actual company-PC scenario). Both regression-tested.
4. Delivery bundle regenerated at `yucode-deliver/` — wheels built fresh, offline-install-tested into a clean venv, full suite (496 tests) run against the bundled source, skill discovery verified from a fake home dir.
5. **Next:** hand `yucode-deliver/` to the owner for the on-site checklist (`WORKBENCH_PLAN.md` §7). After on-site smoke + eval: revisit P2s (WI-7 per-phase reasoning effort, `tools.disabled` tuning, single-mode pytest hook) with real usage data.

## 10. Explicitly not worth doing now

- Deleting weak-tier code, response dedup, or the text-tool-call parser — dormant, tested, and they're the fallback story.
- Switching to the openai SDK, adding a provider registry, or async rewrite.
- Windows-native sandbox (Job Objects / AppContainer) — no admin access anyway; permission modes + safety patterns are the realistic layer.
- Prompt-embedded tool calling (plan contingency C-1) — only if the on-site probe shows the gateway strips `tools`.
