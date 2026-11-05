# Improvement plan — 2026-08-22

**Purpose.** What is left to do to yucode, ranked, with the evidence behind each item stated explicitly. Written to be worked through slowly; nothing here is urgent.

**Companions.** `HARNESS_COMPARISON_2026-08.md` (the analysis this came from — read §8 first), `AGENT_UPGRADE_NOTES.md` (2026-07 GPT-5.5/Workbench review; parts are now stale, see §7 below).

## Evidence legend

Every claim below carries one of these. It is the organising principle of this document, because twice this session a measurement reversed a conclusion that looked obvious:

- **[M]** — measured this session, number cited.
- **[V]** — verified by reading the code, `file:line` cited.
- **[R]** — read in a reference repository's own source or docs.
- **[A]** — assumed, inferred, or from a secondary source. **Not yet evidence.** Do not act on an [A] item without promoting it first.

Two cautionary examples from this session, both [A] that turned out false: grok-build was said to keep goal state in `GOAL.md` (it does not — that file belongs to a third-party package; grok-build is event-sourced), and claw-code was said to have sub-agent swarms (it has none; its own `PARITY.md:113` says so). Both came from blog write-ups. **The reference repositories are readable — read them.**

---

## 1. Where yucode actually stands

Per dimension, against the four harnesses read directly this session. "Behind" only counts where it plausibly affects a single user doing one task at a time.

**The fourth is opencode**, added last and the largest of them: `anomalyco/opencode` (renamed from `sst/opencode`; the archived Go `opencode-ai/opencode` is a different repo), TypeScript/Bun, MIT, ~200k stars, 32-package monorepo, last pushed the same day this was written. [R] It is worth separating from the other three because it is the only one that has directly attacked two problems yucode has.

| Dimension | Standing | Evidence |
|---|---|---|
| Turn-loop shape | **Level.** Maps stage-for-stage onto dsh's `turn/step` flow; the tool pre/execute/post pipeline is already what dsh calls a guarded execution pipeline | [V] `HARNESS_COMPARISON` §1 |
| Loop discipline | **Level or ahead.** 8/8 scenarios clean: no redundant calls, no re-reads, no tool errors, 2–5 iterations per task | [M] 25/25 verdicts, real model |
| Grounding / self-honesty | **Ahead.** Three concrete checks against recorded tool observations (`grep_matched_but_no_read`, `claimed_success_but_bash_failed`, `time_sensitive_no_web_search`). No equivalent found in any of the three | [V] `core/runtime.py:226-281`, [R] absent in all three |
| Behavioural diagnostics | **Ahead.** Per-run timeline, CoT capture, loop-waste metrics, harness prompt introspection. dsh's runtime invariants check protocol correctness; claw-code-agent's benchmarks measure the *model*. Neither measures what the harness does to the model | [R] searched dsh's 44 subsystem docs + 48 packages, grok-build's 24-chapter guide, claw-code's 27 docs and claw-code-agent's benchmarks |
| Provider resilience | **Ahead in one respect** (host-keyed circuit breaker with half-open probe, absent in all three), **behind in three small ones** (no jitter, ignores `Retry-After`, retry table missing 529/520-524) | [V] `core/providers.py:39-86`, `:24-26`; [R] grok-build `xai-grok-sampler/src/retry.rs` |
| Context budgeting | **Behind.** Three thresholds (60k/30k/100k) that do not know about each other and none aligned to the model's window; retention counted in messages, not tokens | [V] `memory/compact.py:92-97`, `core/session.py:272`, `core/runtime.py:970`; [R] grok-build uses 85% of context window, per-model overridable |
| Tool result durability | **Behind.** `bash` spills full output to disk and returns a pointer; nothing else does, so anything past the 32KB cap is lost | [V] `tools/shell.py:37-55` vs `core/runtime.py:1599`; [R] dsh's `ctx.spillStore` and opencode's `tool/truncate.ts` are both policies on *all* tools, hung on one chokepoint |
| **Post-edit verification** | **Absent entirely.** Nothing checks a file after the agent writes it. The only `ast.parse` in the codebase is inside the read-only `file_outline` tool. opencode runs LSP diagnostics after every edit and appends errors to the tool result | [V] `tools/filesystem.py:517` is the only parse, and it is read-only; [R] opencode `tool/edit.ts:197-201` |
| LSP | **Doubly dead.** No production caller ever registers a server, so `dispatch()` always returns `{"error": "No LSP servers registered"}` — and if one were registered it would return the literal string `"LSP {action} result placeholder"`. 221 lines. Header says it is a port of claw-code's `lsp_client.rs`; same ported-shell pattern as the registries | [V] `core/lsp_registry.py:192`, `:204`, grep for `register()` callers = 0 |
| Delegation | **Behind, and possibly negative.** All three let the model decide; yucode decides with a keyword heuristic. Sub-agent results carry no stop reason | [M] see §3.1; [V] `coordinator.py:599-605`; [R] grok-build `stopReason`, dsh typed `SubagentResult` |
| Parallelism | **Behind, does not matter.** `_run_phase` is a sequential `for`. Only grok-build has real parallelism (agent budget 128, 32 concurrent). Affects wall-clock, not answer quality | [V] `coordinator.py:538-558`; [R] |
| Sandboxing | **Behind on Linux, level on Windows.** bwrap on Linux; no OS sandbox on Windows — but grok-build has none either | [V] `security/sandbox.py`; [R] |
| Ecosystem | **Behind, does not matter.** MCP stdio only, no plugin community. On a box that cannot run `npx`, most stdio MCP servers are unusable anyway | [V] `plugins/mcp.py:164-169` |
| Deployability on the target box | **Ahead, and uniquely so.** Pure Python, `python -m`, no npm, no `.exe`. dsh needs Node; grok-build and claw-code ship binaries | [M] dsh would not start locally in two bounded attempts; the `grok` on PATH is a broken shim |

**Read that table honestly.** The dimensions where yucode is behind and it matters are: context budgeting, tool-result durability, and delegation. Everything else is either level, ahead, or irrelevant to one person doing one task.

### What opencode has that the other three do not

Three designs worth knowing about, all [R]:

- **Context Epoch** (`CONTEXT.md`). The system prompt is rendered **once** at the start of an epoch and treated as an immutable provider-cache baseline. Later context changes are not a re-render — they go out as chronological system messages at a safe provider-turn boundary, merged when several arrive together. Only compaction or a session move starts a new epoch. This is the only design across four harnesses that attacks cache-prefix stability head-on, and it is exactly the problem §3.3 describes.
- **Shadow-git snapshots** (`snapshot/index.ts`). `git --git-dir <data dir>/snapshot/<project>/<hash> --work-tree <workspace>` gives track / restore / diff / revert without ever touching the user's index or HEAD. **Pure git CLI — no Node, nothing to install**, so unlike everything else in these repos it ports to Python directly. `session/revert.ts` rewinds to a specific message.
- **Compaction derived from the model's window**, not from constants: `usable() = model.limit.input - reserved`, `reserved = min(20_000, max_output_tokens)`, and the overflow check counts cache reads and writes. No magic absolutes anywhere. [R] `session/overflow.ts`

And one piece of **negative** evidence worth as much as the positive: opencode spends ~146KB of source (`provider/provider.ts` 79KB + `transform.ts` 67KB) normalising differences between providers. That is the price of multi-provider swappability, itemised. yucode has one gateway and should not pay it — this is concrete support for §5's rejection of a provider registry.

### The pattern worth remembering

Everywhere yucode is ahead — grounding supervisor, behavioural diagnostic, circuit breaker — the code was written in response to an observed failure. Everywhere it carried dead weight — 34 tool specs, four registry modules, `parallel_workers`, `lane_events` — the code was ported from claw-code, and in the registries' case the upstream was itself a stub ([R] `PARITY.md:113`). **Porting is where the rot came from; observed failure is where the value came from.** Weight new items accordingly.

---

## 2. Backlog A — confirmed defects

Measured or verified. No further evidence needed; these are just work.

| # | Item | Evidence | Size |
|---|---|---|---|
| ~~A1~~ ✅ | **Sub-agent results carry no stop reason.** Output truncated at `max_worker_steps` is indistinguishable from a completed result, so the parent builds on half-finished work as fact. Both paths return a bare string | [V] `coordinator.py:599-605`, `tools/agent_tool.py:155` | S |
| ~~A2~~ ✅ | **No depth limit on the `agent` tool.** Without a `role`, the child inherits the full tool set including `agent`; recursion is unbounded, one thread per level. grok-build hard-limits depth to 1 | [V] `tools/agent_tool.py:85`; [R] | S |
| ~~A3~~ ✅ | **Tool output over 32KB is lost.** Generalise bash's spill-to-disk-and-return-a-pointer to every tool. `grep_search` over the cap leaves a single line | [V] `core/runtime.py:1599`, `tools/filesystem.py:897` | S–M |
| ~~A4~~ ✅ | **Deny rules are bypassable.** `PermissionRule.matches` prefix-matches the whole command, so `bash(rm -rf *)` stops `rm -rf /` but not `cd /tmp && rm -rf /`. Harmless today only because the rule lists ship empty — **so fix this before writing any rule** | [V] `security/permissions.py:115-120`, tested | S |
| ~~A5~~ ✅ | **Audit rows carry no session id and no cwd**, and the whitelist covers only `bash`/`write_file`/`edit_file` — `apply_patch`, `fill_pptx_*`, `append_word` write files with no trace. `Session.session_id` now exists; wire it through | [V] `observability/metrics.py:91,166` | S |
| ~~A6~~ ✅ | **`eval.py` never reads its own pass criteria.** Every prompt in `eval_prompts.yaml` has `expected:` (`max_tool_calls`, `must_read_files`, `must_not_mention_artifacts`, `acceptable_conclusions`); the runner reads `id`/`category`/`prompt` only, so every eval is graded by hand | [V] `interface/eval.py:175-177`, grep confirms no other reader | S–M |
| ~~A7~~ ✅ | **Headless always exits 0.** A failed run is green in CI, and the JSON carries no usage, session id, or stop reason. grok-build's headless JSON has `stopReason`, `sessionId`, five usage buckets, `total_cost_usd`, and `usage_is_incomplete` flags | [V] `interface/cli.py:1047`; [R] | S |
| A8 | **No stall detection in the validation retry loop.** `_incorporate_feedback()` pastes identical feedback into every work task and reruns up to 3×, even byte-identical. grok-build returns `ClassifierStalled` and auto-pauses | [V] `coordinator.py:456-461`, `:805-813`; [R] | S |
| ~~A9~~ ✅ | **Coordinator workers cannot take an injected provider**, so any external instrumentation of the coordinator path silently misses every worker call. This produced a wrong A/B result this session before it was caught | [M] first A/B run; [V] `coordinator.py:568-573` | S |
| ~~A10~~ ✅ | **Retry gaps:** no jitter, `Retry-After` ignored, 529/520-524 not retryable, and the stall timer is reset by keepalive comment lines rather than content | [V] `core/providers.py:24-26`, `:601` vs `:617`; [R] grok-build retries 15× with ±20% jitter and honours `Retry-After` | S |
| ~~A11~~ ✅ | **`metrics.save()` has no caller** — metrics live and die in memory. Either wire it to the session id from A5 or delete it | [V] grep: zero callers | S |
| ~~A12~~ ✅ | **Four modules are dead**: `lane_events`, `recovery_recipes`, `branch_lock`, `stale_branch` (its only "references" are two same-named string enum values, not imports). Referenced solely by `tests/test_parity_harness.py`. Mark them parity-only or delete; right now the next reader assumes they are live | [V] grep, production callers = 0 | S |
| ~~A13~~ ✅ | **`tool_search` searches nothing.** No tool is deferred, so it returns things already in the request. Either implement deferred loading or drop it | [V] `tools/misc.py:128-136` | S |
| ~~A14~~ ✅ | **`/clear` is destructive** — `messages.clear()` in place. grok-build's `/clear` opens a new session directory instead, so the old one survives | [V] `interface/cli.py:1581`; [R] | S |
| ~~A16~~ ✅ | **Nothing verifies a file after the agent edits it.** A syntax error introduced by an edit is discovered later, if at all — typically by a pytest run several turns downstream, after the model has already built on the broken file. opencode appends LSP diagnostics to the edit's tool result so the model fixes it on the spot. yucode gets most of that from `ast.parse()` on `.py` writes, stdlib only, no LSP and no external linter. **Syntax errors only — not style**: ruff/mypy are not guaranteed present on the locked-down box, and lint noise would bury the real error | [V] no post-write validation exists; [R] opencode `tool/edit.ts:197-201`, `lsp/diagnostic.ts` caps at 20 per file | S |
| ~~A17~~ ✅ | **Delete `core/lsp_registry.py`.** 221 lines that cannot work: no production code path registers a server, and the dispatch result is a hardcoded placeholder string. The `LSP` tool that reaches it is already disabled by default | [V] `core/lsp_registry.py:192`, `:204`; `tools/__init__.py:274` is the only caller | S |

---

## 3. Backlog B — measured signals that need more data

Real numbers exist, but not enough of them to change a default. **Get the data before acting.**

### 3.1 The delegation heuristic may be a net loss on a strong model

Measured, n=3, `deepseek-chat`, single run:

| | single | auto (coordinator) |
|---|---|---|
| succeeded | 2/3 | 2/3 |
| model calls | 14 | **106** |
| input tokens | 141,394 | **791,517** |
| wall time | 21.7s | **204.3s** |

5.6× the tokens, 7.6× the calls, 9.4× the time, for the same success rate. [M]

Routing rate is bimodal: **0 of 8** diagnostic scenarios trip `is_complex_prompt` at strong tier (short factual questions), but **7 of 10** realistic development requests do — `_COMPLEXITY_KEYWORDS` (implement / build / analyze / refactor / create / 調查) bypasses the six-word gate outright. [M]

Compounding it, the validator never looks at the workspace: `_validate()` and three sibling calls pass `tools=[]`, so it is a text-only judge reading the worker's own account of itself. `ROLE_TOOLS[VALIDATE]` is unreachable. [V] `coordinator.py:484,499,667,780`

**Before changing `orchestration_mode` or the keyword list:** run the A/B harness (in scratchpad; productise as A15 below) at n ≥ 10 on the company box against GPT-5.5. Note `classify_reasoning_effort()` shares `is_complex_prompt`, so the two move together. A 5.6× cost multiplier is worth a day of measurement.

### 3.2 Compaction thresholds are un-calibrated

Three thresholds, mutually unaware, all Qwen3-era absolutes: 60k (loop-level, `len//4` estimate), 30k (request-time pruning, same estimate), 100k (post-turn, real `input_tokens`). No `context_window` concept exists anywhere in the codebase. [V]

grok-build triggers at 85% of the model's window, per-model overridable, and retains by token budget without splitting a turn. [R] yucode retains `compact_preserve_recent = 4` **messages** — one large tool-call round can consume the entire retention window. [V] `config/settings.py:137`

opencode does the same thing as grok-build and shows the shape clearly: `usable() = model.limit.input - reserved` with `reserved = min(20_000, max_output_tokens)`, and the overflow check counts cache reads and writes. No constants at all. [R] `session/overflow.ts`

**Blocker, and it comes first.** Both designs need one number yucode cannot currently obtain: **`providers.py` has no concept of a model's context limit** — nothing in `ProviderConfig` or the response parsing carries it. [V] So this item is not "change the thresholds", it is "acquire the number, then change the thresholds", and the number has to come from somewhere: a hand-maintained per-model table, a `provider.context_window` config field the operator fills in, or a gateway that reports it.

**Before acting:** (1) confirm compaction ever fires on real workloads — `_maybe_auto_compact` gates on the last response's `input_tokens`; log it for a week of real use, and if it never fires this is theoretical; (2) decide where the window number comes from. If the answer is "guess a constant", **stop** — a wrong window is worse than today's honest absolutes.

### 3.3 Prompt caching may be unreachable

Capping the git diff fixed the **size** of the prefix problem (81,170 → 26,478 chars, ~13,700 tokens/request [M]). It did not fix the **stability** of it, and the stability is the part caching depends on.

Measured with the workspace unchanged and git context off, two consecutive turns still render **different** system prompts [M] — because `estimated_tokens` and `resumed_messages` are passed into `PromptAssembler` and rendered into the prompt body as `"Context usage: ~9,500 estimated tokens (95% of compaction threshold)"` and `"Session: RESUMED — 4 prior message(s)"`. [V] `core/runtime.py:573-578`. Both change every single turn by construction. So the cache prefix is invalidated on **every** turn regardless of what the user does.

`cached_tokens` is already parsed and displayed. [V] `core/providers.py:207`, `interface/render.py:932`

Two designs to copy, if the number justifies it. dsh's `PromptContext` makes volatile content a post-history user-role snapshot re-sent only when it changes. opencode's **Context Epoch** goes further: render the system prompt once as an immutable baseline and deliver every later change as a chronological system message at a safe turn boundary, starting a new epoch only on compaction or a session move. [R] Either way the fix is the same shape — **move the volatiles out of the system message** — and yucode already has half of it: `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` marks the exact split point and has no consumer. [V] `memory/prompting.py:13`, `:87`

**Before acting:** read `cached_tokens` on the real gateway. If it is always 0, there is no prefix caching in front of Workbench and this item is dead — in which case delete the boundary marker instead, since it is currently a literal `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` sitting in every request.

---

## 4. Backlog C — structural, worth doing on their own merits

| # | Item | Rationale | Size |
|---|---|---|---|
| A15 | **Productise the orchestration A/B harness.** It measures the coordinator path, which `diagnose_agent.py` cannot: that calls `run_turn()` directly at `:858` and bypasses `orchestrate()` entirely [V]. Needs A9 to be accurate | Gate for every §3 decision | S |
| C1 | **Extract the turn-stopping supervisors.** Give the grounding check and todo gate the treatment `_collect_post_tool_reminders` already has — a list of checks with a shared budget, called from one place. Shrinks `run_turn()` (410 lines, five interleaved mechanisms), makes each unit-testable | **Only when a third stop-check is actually wanted.** Not speculatively | M |
| C2 | **Store the rendered system prompt in archives too.** It now lives on the session and in checkpoints; `_archive_before_compact()` still omits it | Completes the forensics story | S |
| C3 | **Rewrite the `bash` tool description to state its contract:** fresh shell per call, `working_directory` rather than `cd`, cmd.exe on Windows, safety refusals are policy not a retryable error, long output already spilled to the returned path. Note the inversion of received wisdom — core tool descriptions are *too short*, not too defensive (`read_file` is 36 characters, shorter than dsh's) | dsh puts exactly this in its bash description [R] | S |
| C4 | **One line of prompt-injection defence.** There is none today, and `web_fetch` returns cleaned page text with no delimiter [V] `tools/web.py:120-160`. claw-code's entire defence is one system-prompt sentence [R] `prompt.rs:692`; yucode does not even have that. Add it plus an `<untrusted-content>` delimiter | Cheapest security item left | S |
| C5 | **Job Object process limits on Windows.** `CreateJobObjectW` + `KILL_ON_JOB_CLOSE` + process/memory caps, ~150 lines of `ctypes`, **no admin required** — this corrects `AGENT_UPGRADE_NOTES` §10, which dismissed Job Objects together with AppContainer on the grounds that there is no admin. Stops fork bombs, runaway memory, orphaned process trees after a timeout | **Only if a runaway process has actually happened.** Otherwise speculative | M |
| C6 | **Clean up test residue.** 240 workspace state directories under `~/.yucode/projects/`, 199 with audit logs, all from test and diagnostic runs [M]. They make the audit trail meaningless by mixing real and test runs in one tree with no way to tell them apart | Pairs naturally with A5 | S |
| C7 | **Shadow-git snapshots instead of finishing `checkpoint()`.** opencode runs `git --git-dir <data dir>/snapshot/<project>/<hash> --work-tree <workspace>` to track, diff, restore and revert the workspace without ever touching the user's index or HEAD. [R] `snapshot/index.ts` Pure git CLI, so it ports to Python as-is — and it is simpler and more useful than serialising session state, which is what `checkpoint()` half-does today (write-only, no restore path). **Not now**: checkpoints have never been written in real use [M], so make them used before making them better | Replaces, not extends, `runtime.py:1147-1176` | M |

---

## 5. Explicitly not doing

With the reason, so this does not get re-litigated:

- **Event-log migration / dsh's projection architecture.** The valuable parts (system prompt recorded, session id, correlatable archives) are already done as a strict subset. The rest is a rewrite for a single-user deployment.
- **Cordis-style everything-is-a-plugin, capability seams, provider registry.** Buys swappability across deployments, providers, and product surfaces. yucode has one of each. Also: dsh is a two-week-old v0.1 RC whose own README promises breaking changes — porting its structure means chasing a moving target. Port its *ideas*, not its shape.
- **Windows ACL sandbox.** Feasible without elevation (dsh's `sandbox-windows-acl` copies the caller token to `WRITE_RESTRICTED` and edits the workspace DACL via the owner's implicit `WRITE_DAC`) [R], but risk-asymmetric: write-only protection, known Everyone and hard-link holes that dsh itself marks `enforcement: 'partial'`, and dsh needs a C++ ABI probe to validate struct layout before shipping. Hand-rolled in `ctypes` without that, the likely failure mode is fail-open with a full token — worse than no sandbox. grok-build has no Windows sandbox either.
- **MCP HTTP/SSE transport, tiktoken, the openai SDK, `sessions.fork()`, ACP, TUI parity, web UI, external telemetry.** No consumer today; several violate the no-npm / no-compiled-dependency constraint.
- **A provider registry / multi-provider abstraction.** opencode itemises the price: ~146KB of source normalising provider differences (`provider/provider.ts` 79KB + `transform.ts` 67KB). [R] One gateway, one model — do not pay it.
- **opencode's CodeMode** (model writes JS that orchestrates tools, run through a hand-written tree-walking interpreter). Clever, and the wrong shape for a Python runtime with one user.
- **A real LSP client.** A16 gets the valuable part — errors fed back at edit time — from `ast.parse()`. Spawning language servers and speaking JSON-RPC is a large build for the remainder, and the existing 221-line stub is being deleted (A17), not finished.
- **Real parallelism.** Affects wall-clock only. Revisit if a real task ever takes long enough to care.
- **Wiring up `check_bash()` and `bash_validation.py`.** They are dead [V], and connecting them would be actively harmful: `_READ_ONLY_COMMANDS` includes `python -c`, `node -e`, and `awk`, so a working `read-only` mode built on them would permit arbitrary code execution while claiming not to.

---

## 6. Sequence

Slowly, gated on evidence, one at a time.

> **Status 2026-08-22 (later the same day).** Everything in the *Now* and *Next* tiers below is done except A8, plus C2, C3, C4 and C6. Suite 789 → 892 tests, no failures; the behavioural diagnostic still reports 25/25. Items are struck through in the tables above. What is left: **A8** (validation stall detection), **A15**, **C1**, **C5**, **C7**, and everything in §3, which is gated on measurements that have to happen on the company box.
>
> Three things surfaced while doing the work and are **not** yet in the tables:
> - **`policy_engine.py` and `green_contract.py` have zero production callers** — the same dead-parity pattern as the five modules just deleted. Verify and remove in the next pass.
> - **Streaming failures are never retried.** `_parse_streaming_response` raises `ProviderError` from inside the `try`, where neither the `HTTPError` nor `URLError` handler catches it, so one stall costs the whole turn. Probably the largest remaining resilience gap.
> - **A sustained 429/503 never opens the circuit breaker**, because every `HTTPError` path calls `_breaker_record_success()`. An overloaded gateway is paid for in full on every call.
>
> And one known imprecision introduced by A1: the worker marker keys off `iterations >= max_worker_steps`, so a worker that finishes on its very last step is labelled INCOMPLETE. `TurnSummary` has no stop-reason field to key off instead. The error direction is conservative (over-verify rather than trust), which is why it shipped.

**Now — no new evidence needed.** A16 first (post-edit syntax check: new capability, stdlib only, catches a class of error that currently costs a whole round-trip to find), then A1, A2, A4, A5, A9, A12, A17. All small, all confirmed. A4 before writing any permission rule; A9 before any coordinator measurement.

**Next — cheap and clearly useful.** A3, A6, A7, A10, A11, A13, A14, C2, C3, C4, C6.

**Then — gated.** A15 first, then measure §3.1 at n ≥ 10 on GPT-5.5. Only then touch `orchestration_mode` or the keyword list. §3.2 and §3.3 need their own readings first (does compaction fire? is `cached_tokens` ever non-zero?).

**Only on demand.** C1 when a third stop-check is wanted. C5 if a runaway process actually happens.

**Housekeeping, any time.** `v0.6.0` is still tagged locally only and now far behind `main` — drop it or re-tag. `CLAUDE.md` is still deleted in the working tree, uncommitted.

---

## 7. What would change this plan

- **A real-model A/B at n ≥ 10 contradicting §3.1.** If the coordinator earns its 5.6×, delegation moves from a liability to a strength and C1 becomes worth doing properly.
- **`cached_tokens` reading non-zero.** Promotes §3.3 from theoretical to a straightforward cost win.
- **Compaction never firing in a week of real use.** Demotes §3.2 to noise.
- **A second person using yucode.** Would change the calculus on §5's whole list — most of those are "not for one user" rather than "not good".
- **Parts of `AGENT_UPGRADE_NOTES.md` are stale.** Its §1 P0s (400 self-heal, Azure routing) are done; its §4 Windows gaps are two-thirds fixed and the third reversed into the opposite bug; its §10 dismissal of Job Objects is wrong. Verify before citing it.

---

## 8. Second pass — 2026-08-25

Everything in §6's *Now* and *Next* tiers is done, plus C2, C3, C4, C6 and the extra items below. Suite 789 → 1060, diagnostic still 25/25, `core/` down from 21 modules to 14.

### What the review found, and why it matters more than the count

An adversarial review ran over the day's own fixes. Fifteen findings; **seven were defects introduced by those fixes**, and two were pairs of fixes that cancelled each other out. That ratio is the useful number here, not the fifteen.

The two cancellations are worth naming, because both halves were individually correct:

- `probe_tool_calling` was added so `doctor` would report the gpt-5.6 tools rejection. A self-heal for that same 400 was added hours later, in a different file, by someone who could not see the probe. The heal fixed the error inside `complete()`, so the probe saw success — and the user was never told reasoning had been switched off for the session.
- `_stop_reason` was taught to fail a headless run on the harness's own truncation markers. The `[INCOMPLETE:` banner was added the same day and was not one of them, so CI stayed green on output the harness had just labelled partial.

Neither is visible from inside either change. Both were found by reading the whole diff at once.

### The recurring shape: built, tested, never connected

Three times in one day, a mechanism was written completely, tested green, and wired to nothing:

| | |
|---|---|
| `set_default_policy` | The whole `EnvScrubPolicy` mechanism existed with zero callers, so a user's `shell_env_allowlist` still reached bash and nothing else — the exact bug it was built to fix. |
| `MetricsCollector.save()` | Had no caller at all. And once called, the coordinator path wrote a snapshot for every worker and **none for the parent**. |
| `_persist_metrics` on the coordinator path | See above — fixing "writes too many" uncovered "never wrote the one that mattered". |

This is the same shape as the seven ported modules deleted earlier in the week — `policy_engine`, `green_contract`, `lsp_registry` and friends, all complete, all unreachable. The difference is only age: those were months old, these were minutes old.

**A green suite does not distinguish the two.** That is the argument for this document's evidence legend, and for reviewing diffs rather than trusting per-change test results.

### Security items, stated at their real strength

- The spill exemption added to `_resolve_path` initially granted **writes** to the whole agent state tree, because every write tool shares that method. `write_file` could overwrite the audit log the same branch had just added session stamping to. Now read-only, opt-in per call, and scoped to the two spill directories.
- Deny-rule bypasses via `sudo -u`, `sh -c`, `xargs` and nesting are fixed. **Still bypassable, verified rather than assumed:** `/bin/rm -rf /` (absolute path defeats prefix match), `rm -r -f /` (split flags), `$(...)` substitution, here-strings, `xargs -I{}`. A prefix-matching deny list has a ceiling; this is the ceiling, not an oversight.
- `<untrusted-content>` marking now covers the spilled artifact and every `offset/limit` excerpt of it, capped so truncation cannot eat the closing tag. `grep_search` and `bash` reads of that file are still unmarked.

### Still open

Carried from §6, unchanged: **A8**, **A15**, **C1**, **C5**, **C7**, and all of §3 — every one gated on a measurement that has to happen on the company box.

Added by this pass, then closed in the same pass: state-directory retention, the JSON total cap, sub-agent audit identity, and the bridge session key.

## 9. The target model turned out to be DeepSeek — 2026-08-26

The company box will run DeepSeek V4 Flash, not GPT-5.5. That retires the caveat this document has carried since §6: measurements taken here against a DeepSeek endpoint are now *representative* rather than a stand-in, and the whole §3 backlog stops being blocked on hardware nobody has.

Asking the gateway rather than reasoning about it found three things, all [M]:

- **[M] Token estimation counted characters and divided by four.** Measured on material this agent actually carries — its own source, a real `git show -p`, `list_directory` JSON, English markdown, Chinese prose — characters per token ranges 1.34 (Chinese) to 3.98 (Python), a 2.97x spread that one divisor cannot cover. A Chinese session was rated at a third of its size and reached the nominal 60k threshold holding roughly 180k. Estimating from UTF-8 bytes collapses the spread to 1.57x, because a CJK character is three bytes and about one token while an ASCII character is one byte and about a quarter. One constant, no script detection. Fixed.
- **[M] The gateway caps replies at 8192 output tokens when `max_tokens` is absent**, and it was absent — `ProviderConfig` had no such field. It accepts up to 393216. About 28KB of generated text, which one large `write_file` clears. Fixed, unset by default; a 400 that names its own ceiling is now learned and clamped to.
- **[M] Truncation was invisible.** Both parse paths read `finish_reason` and neither kept it, so a reply severed at the cap was indistinguishable from a finished one, and the tool call it carried arrived as unparseable JSON answered with "fix the JSON syntax and retry" — advice that cannot be followed. Fixed at all three points.

Two entries below were **wrong**, and are recorded as such rather than quietly dropped:

- The **120s transport stall timer being reset by keepalives is the design**, not a defect. Keepalives do prove the socket is alive, which is what that timer asks; the 300s content timer is the one that asks whether anything is being produced, and it correctly ignores them. Genuine reasoning output resets it, so 300s of silence is a hung connection. No change.
- **`grep_search` reading web artifacts unmarked** was the small half of a much larger problem. `search_path` went to `rg` without passing through `_resolve_path`, so `grep_search` read *any* file the process could — verified against a directory outside the workspace, where `read_file` refused and `grep_search` printed the contents. An audit of every path-taking tool found it was the only one. Fixed, and resolving without `allow_spill_read` closes the artifact-marking gap as a side effect.

## 10. Prompt caching, and a hypothesis that was wrong — 2026-08-26

DeepSeek serves a cache hit at roughly a tenth of the price of a miss, and it matches on a prefix, so anything that rewrites the front of a request throws away everything paid for behind it. Measured over a three-turn session that reads two real source files:

| configuration | cache hit |
| --- | --- |
| `compact_token_threshold: 10000`, this repo's shipped user config | **23%** |
| `compact_token_threshold: 120000` | **36%** |

**[M] The threshold is the miscalibrated parameter.** The endpoint accepted a **220,007-token** prompt, so 10,000 compacts at under 5% of what the model can hold. Every time compaction fires it replaces the history with a summary — the request's whole front — and the cache restarts from the system prompt. `prune_tool_results` does the same thing at `threshold // 2`, which at 10,000 means it is rewriting requests from 5,000 tokens onward. Both were confirmed in a message-by-message hash of the request stream: the turn where compaction ran is the turn the hit rate collapses to 1,920 tokens and stays there.

This also interacts with §9. The estimator now counts honestly, so a threshold that was calibrated against a 3x under-count for Chinese now fires roughly 3x sooner in a Chinese session. **The user's `compact_token_threshold` needs raising, and it is their file to change.**

**[M] And a hypothesis that did not survive.** The volatile half of the system prompt — environment block, git state, the per-turn context counters — sits near the top, ahead of the whole conversation, which looked like the obvious cache killer. It was moved to a trailing message after the conversation and measured against the original placement on the same workload: **both cached 31,360 tokens, byte for byte.** No effect. The change was reverted rather than kept on the strength of the reasoning.

One piece of it was kept, because it is a defect independent of caching: `render()` joined straight through `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`, so the literal string `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` was shipped to the model in every request. It is a marker for people and is now dropped on the way out.

What remains unexplained is why a hit rate of 36% and not more, once nothing is rewriting the prefix. The prefix grows monotonically across those turns and each call should be re-using the last one's. Not chased further; it needs the gateway's own semantics, and no local change is indicated by the evidence in hand.

### Still open

Carried from §6: **A8**, **A15**, **C1**, **C5**, **C7**. §3 is no longer blocked — see §9.

- **Streaming retry only covers pre-output failures** — a long answer that stalls mid-way is still lost. Deliberate: replaying after output would double-print in the terminal.
- **The env-scrub policy is process-wide.** The worst instance is fixed (`/tools` no longer builds a runtime, so listing tools no longer re-points credential withholding for every workspace), but a bridge serving two workspaces with different `shell_env_*` settings still has the last runtime constructed win for both.
- **The 400 self-heal spends a retry slot per correction.** `_MAX_SELF_HEAL_ROUNDS` is 4 against 5 attempts, so a gateway needing four corrections has one real retry left.
- **Deny-rule bypasses that a prefix matcher cannot close:** `/bin/rm -rf /`, `rm -r -f /`, `$(...)` substitution, here-strings, `xargs -I{}`. Verified, not assumed. Closing these needs a different mechanism, not a longer list.
- **`bash` reads of a web artifact are unmarked.** Unlike `grep_search` this cannot be fixed by a path check — bash output is arbitrary text — so it needs either a different mechanism or an explicit decision to accept it.

### And the standing caveat

`diagnose_agent.py` drives `run_turn()` directly, so **the coordinator path still has no behavioural coverage at all** — and it is the component measured at 5.6x the tokens for the same success rate. That is now the single largest untested surface, and nothing external blocks testing it. See [[yucode_measurement_gap_2026_08]] in the session notes.
