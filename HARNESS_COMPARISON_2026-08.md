# Harness comparison — yucode vs. dsh / grok-build / claw-code (2026-08-15)

**Audience:** the owner, and any implementing agent doing follow-up work.
**Scope:** a段-by-段 comparison of yucode's turn loop against DeepSeek Harness's documented `turn/step` event flow, plus what grok-build and claw-code add. Every yucode claim cites a line as of commit `4114b14`; re-verify before editing.
**Companion docs:** `AGENT_UPGRADE_NOTES.md` (GPT-5.5/Workbench era review), `WORKBENCH_PLAN.md`.

> **Status 2026-08-22.** A six-domain follow-up review (tools, orchestration, context, security, provider, observability) ran against this document and found more than it did. Most of §6's P1 list is now done — see **§8 What changed** at the end for what landed, what the numbers were, and what is still open. Read §8 before acting on §6.
>
> Two claims in the original draft came from secondary sources and did **not** survive reading the repositories. They are corrected in place below: grok-build has no `GOAL.md`, and claw-code has no sub-agent swarms.

---

## 0. The headline

yucode's loop is **structurally closer to dsh than the feature lists suggest**. Almost every stage dsh formalises as an event already exists in `run_turn()` as a hardcoded call site. The gap is not missing capability — it is that **the extension points are inline, so the loop accretes**.

`run_turn()` is 410 lines (`core/runtime.py:543-953`) and currently interleaves five independent supervisor mechanisms, each with its own state and fire budget. Adding a sixth means editing the loop again. That is the actual cost being paid, and it is the lens for everything below.

---

## 1. Stage-by-stage map

dsh's documented flow (from `docs/architecture.md`, *Turn flow*) against yucode:

| dsh stage | yucode equivalent | Notes |
|---|---|---|
| `turn/start` | — | No turn-open event. `run_turn()` just starts. |
| claim input + queued message | `run_turn(prompt)` argument | Single input, no inbox. No queued-message concept. |
| assemble prompt sections + tool schemas | `discover_project_context()` + `PromptAssembler(...).render()` (`:559-578`), `tools.definitions_for_provider()` (`:665`) | Present and well-factored. Prompt sections are the closest thing yucode has to a real seam. |
| `agent/pre-step` (waterfall, may rewrite or reject) | `effective_prompt = prompt + weak-tier nudge` (`:582-592`) | **One hardcoded `if`.** Rewrite-only; no reject path; no way for a plugin to join. |
| `step/start` | `event_callback({"type": "iteration_started"})` (`:638-639`) | Observability only — listeners can't intervene. |
| append entered messages as `user/message` | `session.add_message(Message(role="user", ...))` (`:593`) | |
| derive model history from the log | `session.provider_messages(system_prompt, pruning=...)` (`:664`) | **This is the biggest divergence — see §2.** |
| `agent/request` → `llm/stream` → `assistant/chunk*` | `provider.complete(..., stream_callback=event_callback)` (`:663-669`) | Streaming reaches the UI; no interception seam. |
| `assistant/message` | `session.add_message(assistant_message)` (`:700-705`) | |
| `tool/call*` | `for tool_call in response.tool_calls` (`:786`) | |
| `tools/pre-execute` | `_execute_tool`: enforcer → policy → `pre_tool_use` hook (`:1508-1542`) | **Genuinely good.** Three independent gates, hook can deny. Closest thing yucode has to a real waterfall. |
| `tools/execute` | `self.tools.execute(...)` (`:1556`) | |
| `tools/post-execute` | `post_tool_use` hook → secret redaction → 32 KB cap (`:1576-1600`) | |
| `tool/result*` | `session.add_message(Message(role="tool", ...))` (`:896-901`) | |
| `step/end` | — | No step-close event. |
| `agent/turn-stopping` (serial, may continue the turn) | **Two hardcoded blocks:** grounding supervisor (`:712-729`), todo turn-end gate (`:733-760`) | Each has its own separate fire budget (`forced_retry_done`, `todo_gate_fires_this_turn`). Can't be reordered, disabled independently, or tested in isolation. |
| `turn/end` | `return summary` (`:784`) | |
| — | `_maybe_auto_compact()` (`:783`, `:933`, `:952`) | Called at three different exit points. |

**Read that table as good news.** The pipeline shape is right. `_execute_tool`'s pre/execute/post structure is already what dsh calls a guarded execution pipeline. What's missing is that the *turn-level* stages never got the same treatment the *tool-level* ones did.

### 1a. The five interleaved supervisors

These all live inside `run_turn()` and all mutate turn flow:

1. **Grounding supervisor** (`:712-729`) — injects a violation message, `continue`s the loop. Fires ≤1× per turn (`forced_retry_done`).
2. **Todo turn-end gate** (`:733-760`) — injects open todos, `continue`s. Fires ≤`todo_gate_max_fires_per_prompt`.
3. **Dedup escalation** (`:810-841`) — replaces the tool result with an escalating warning; force-stops at 3 blocks (`:914-934`).
4. **Consecutive-readonly advisory** (`:883-891`) — appends an advisory to the tool result at exactly 8 reads.
5. **Post-tool reminders** (`:893-894` → `_collect_post_tool_reminders`, `:1174`) — background tasks, subagents, todo nudge, AGENTS.md discovery, skill discovery. This one *is* factored out, and it shows: adding a sixth reminder there is a one-line change.

Mechanism 5 is the proof of concept. It is the only one of the five that was extracted, and it is the only one that is cheap to extend. **The refactor worth doing is applying that same treatment to 1 and 2.**

---

## 2. The one real architectural gap: no event log

dsh's central invariant is **"model-visible means logged"** — anything reaching a model request must be reconstructable from the append-only `SessionEvent` log, with a runtime assertion enforcing it. `deriveMessages()` projects model history *from* the log; fork, resume, transcripts, telemetry, and persistence all fall out of that one stream.

yucode's `Session.messages: list[Message]` is simultaneously the storage, the model input, and the audit record. Five concrete consequences, each observable in the code:

**2a. The system prompt is never recorded anywhere.**
`PromptAssembler(...).render()` (`:573-578`) produces the largest single model-visible input — instruction files, skills, memory index, office routing, web-freshness section, platform notes. It is a **local variable**, passed to `provider_messages()` and then discarded. It is not in `session.messages`, not in `_archive_before_compact()`'s dump (`:1080`), and not in `checkpoint()`'s dump (`:1141-1155`).

Consequence: **a saved yucode session cannot reproduce what the model actually saw.** This is why `diagnose_agent.py` needed a separate "harness introspection" pass (commit `8ffdff2`) — it had to re-render the prompt out-of-band rather than read it from a trace. Any future "why did it do that" investigation hits the same wall.

**2b. Compaction is destructive, and the archive is a workaround for that.**
`self.session.messages = result.compacted_messages` overwrites in place (`:983`, `:1053`). `_archive_before_compact()` (`:1061-1086`) exists purely to write the pre-compaction state to a JSON file first. With an append-only log, compaction is a *projection* choice — you'd derive a shorter message list from the same intact log — and the archive machinery is unnecessary.

Note `provider_messages`' tool-result pruning got this right (`session.py:255-267`): it is explicitly request-time-only and never mutates `self.messages`. That's the correct instinct, applied in one place but not the other.

**2c. Supervisor injections are indistinguishable from user input.**
Grounding violations (`:720-723`) and todo-gate nudges (`:744-753`) are appended as `role="user"`. On reload, replay, or compaction, they read as if the human typed them. The `[SYSTEM SUPERVISOR]` text prefix is the only marker, and it is inside the content string. dsh avoids this by giving each a distinct session event type that *renders into* a message.

**2d. `reasoning_content` is emitted and dropped.**
`:691-698` — correctly not sent back to the provider, but also not stored. The comment says so explicitly. CoT is therefore unrecoverable after the fact except via a live `event_callback` trace.

**2e. `checkpoint()` is write-only.**
`:1129-1159` writes label, timestamp, token estimate, metrics, and messages. There is **no restore function** — `grep -rn checkpoints coding_agent/` returns three hits, all inside the write path. It also omits the system prompt, the `UsageTracker` state, `_session_edited_paths`, and todo state, so even a hand-written restore would resume into a different context than it left.

---

## 3. Goals — yucode has the parts, not the wiring

Both references converge on the same idea. grok-build's `/goal` is a run-until-done objective with a **verifiable completion condition** and an `update_goal` tool; it ends on verifier-pass, `blocked_reason`, or manual clear. dsh exposes the same thing as `ctx.goals`.

> **Correction.** An earlier draft said grok-build keeps goal state in an external `GOAL.md` that survives compaction. It does not — searching the repository returns no such file; that convention belongs to `cobusgreyling/goal-engineering`, a third-party utility package, not to xAI. Both grok-build and dsh keep goal state **event-sourced** (`xai-grok-session-events`; dsh's `goal/change` events with a strict fold). So the axis is not "external file vs context" but **provenance**: an event log records which turn changed what against which revision and replays deterministically, while `state_dir/todos.json` is last-write-wins and cannot reconstruct the order.
>
> grok-build's `update_goal` is the part worth studying: the model self-reports `completed: true` and an independent classifier verifies it, returning `ClassifierNotAchieved`, `ClassifierCapReached`, `ClassifierBlocked`, or **`ClassifierStalled`** — the same flagged gaps as the previous round, which auto-pauses early. yucode's retry loop (`coordinator.py:456-461`) has no stall detection: `_incorporate_feedback()` pastes the same feedback into every work task and reruns up to three times even when it is byte-identical.

yucode already has all three ingredients — they're just not connected:

| Ingredient | yucode today | Gap |
|---|---|---|
| External state surviving compaction | `state_dir/todos.json` via `todo_write`; read by `_read_open_todos()` (`:1290`) | None — this works. |
| Stopping condition tied to that state | Todo turn-end gate (`:733-760`) | **`todo_gate_enabled` defaults to `False`** (`config/settings.py:165`). The mechanism exists and ships off. |
| Verifier | `_run_concrete_validators()` (`coordinator.py:685`) — runs `pytest -x -q` when `tests/` has test files, and correctly distinguishes "tests failed" from "pytest couldn't start" (`:718-722`) | **Multi-mode only.** In single mode nothing verifies a "done" claim except the bash-returncode grounding check. |

So the honest framing: **yucode does not need a goals subsystem.** It needs the todo gate turned on by default and the validator reachable from single mode. That is a much smaller change than "port `/goal`", and it lands the same capability.

`AGENT_UPGRADE_NOTES.md` §6 already identified the second half of this ("the one coordinator feature worth porting to single mode: `_run_concrete_validators`") and correctly flagged it as opt-in-config work. That judgement still holds; this comparison just confirms it's the highest-value item on the list.

---

## 4. What NOT to port

**Cordis-style everything-is-a-plugin.** dsh's profile/bundle composition, capability seams (Service Definition / Provider / Consumer), and reversible plugin effects are the most intellectually attractive thing in any of the three references. They buy swappability across deployments, providers, and product surfaces. yucode has **one** deployment, **one** provider, **one** user. The refactor cost is a rewrite; the benefit is zero today.

There is one seam violation worth noting only because it marks where a boundary *wants* to be: `runtime.py:1346` and `:1414` call `self.tools._resolve_path(...)` — a private method on the tool registry — from the discovery reminders. If path resolution ever needs to change (remote workspace, sandbox), that's the first thing that breaks. Fix it by making the method public when you next touch it; don't build a seam framework around it.

**Also skip:** ACP / editor embedding, TUI parity, web UI, `sessions.fork()`. All are surface-area for a user base of one.

> **Correction.** An earlier draft credited claw-code with sub-agent "swarms" running in isolated contexts over shared memory. Reading the repository, there is no such thing: `rust/crates/runtime/src/` has no subagent or swarm module, "swarm" appears only twice in `src/reference_data/subsystems/*.json`, and claw-code's own `PARITY.md:113` says its team/cron support "still stop[s] short of a real background scheduler or worker fleet". That matters here because yucode's `task_registry` / `worker_registry` / `team_cron_registry` / `lane_events` were ported from it — **the upstream was already a shell**. Of the three, only grok-build has real parallelism (an `agent_budget` defaulting to 128, 32 concurrent host-side).

**And note dsh's own warning:** it is a v0.1 developer preview, two days old, with "THERE WILL BE COMPATIBILITY-BREAKING CHANGES" in its README. Porting its architecture now means chasing a moving target. Port its *ideas* (the log invariant), not its structure.

---

## 5. What yucode has that the references don't advertise

Worth recording so it doesn't get refactored away in a parity chase:

- **The grounding supervisor** (`:226-281`). Three concrete checks — `grep_matched_but_no_read`, `claimed_success_but_bash_failed`, `time_sensitive_no_web_search` — run against accumulated tool observations rather than model self-report. I found no equivalent in dsh's or grok-build's public docs. It exists because it was built against a weak local model, and it is yucode's genuine original contribution.
- **Within-turn read-only tool cache** (`:851-864`) — keyed on canonicalised args, gated on `permission_for(...) == "read-only"`.
- **Canonicalised-args dedup with escalation and force-stop** (`:799-841`, `:914-934`) — the `json.dumps(..., sort_keys=True)` normalisation at `:800-804` specifically defeats the whitespace/key-order variation that weak models emit.
- **JSON-structure-preserving tool-output truncation** (`_truncate_tool_output`, `:108-139`) — shrinks the largest string leaves rather than byte-slicing, so downstream `json.loads()` of a capped result still works.
- **Tool-result pruning by turn age** (`session.py::_prune_tool_results`) — grok-build parity, already ported, request-time-only.

---

## 6. Recommended order

Ranked by value ÷ cost, given the strategic decision to stop chasing general-purpose parity.

**P0 — measure before building.**
`diagnose_agent_trace.json` records `run.model = "mock-model"`. The behavioural diagnostic has **never run against real GPT-5.5**. Every item below is a guess about what to fix until that number exists. Run `diagnose_agent.py` on the Workbench box first.

**P1 — connect the goal loop (§3).** Turn on `todo_gate_enabled` by default; make `_run_concrete_validators` reachable from single mode as opt-in config. Small, uses existing parts, directly serves the FDD workload where "done" is verifiable.

**P1 — record the system prompt (§2a).** Store the rendered prompt on the `Session` and include it in `_archive_before_compact()` and `checkpoint()`. This is a handful of lines and it's the single highest-leverage fix in the whole document: it makes every future behavioural investigation possible. It is a strict subset of the event-log idea, with none of the rewrite cost.

**P2 — extract the turn-stopping supervisors (§1a).** Give mechanisms 1 and 2 the same treatment `_collect_post_tool_reminders` already has: a list of checks with a shared fire budget, called from one place. Shrinks `run_turn()`, makes each check unit-testable, and means the *next* supervisor is a one-line change. Do this only when a third stop-check is actually wanted — not speculatively.

**P2 — close the checkpoint loop (§2e).** Add restore. Include system prompt, usage, and edited-paths in the dump. Only worth it once checkpoints are actually being taken in real use.

**Don't do:** full event-log migration, Cordis-style seams, anything in §4.

---

## 7. Loose ends found during this review

Unrelated to the comparison, but real and cheap:

- **`v0.6.0` tag is local-only.** Remote's newest is `v0.5.0`; the tag points at `60659a5` (2026-05-17), now 12 commits behind `main`. Decide: drop it, or re-tag HEAD as `v0.7.0` and push.
- **`CLAUDE.md` is deleted in the working tree, uncommitted.** Commit the deletion or restore the file.

---

## 8. What changed — 2026-08-22

A six-domain follow-up review ran one agent per area (context, tools, orchestration, security, provider, observability), each reading the three reference repositories directly rather than secondary write-ups. It found more than §1–§7 did, and the theme was not "yucode lacks features":

**Roughly a third of yucode's surface had never been executed.** Not "written badly" — never run. 84 tool schemas of which 35 had empty `properties` and 11 were no-ops answering `{"status":"pending","message":"acknowledged"}`; four registry modules with no execution backend; a `parallel_workers` option parsed, documented, and read by nothing; `metrics.save()` with no caller; the `expected:` blocks in `eval_prompts.yaml` never read; `PermissionEnforcer.check_bash()` and the whole `bash_validation.py` classifier dead; 240 workspace state directories with 199 audit logs and **zero** sessions, archives, checkpoints or metrics.

That reframes the gap to the three references. It is not a feature gap. Deleting was the highest-value work available.

### Landed

| | Measured before → after |
|---|---|
| Extended tool specs opt-in (`tools.extended_tools_enabled`, default off) | 84 → **51** tools; 35 → **1** empty schema |
| Name-index collision (`structuredOutput` routed to the no-op stub) | camelCase now reaches the real tool |
| `AskUserQuestion` moved to builtins with a real schema + non-TTY guard | was unusable (stub params) and could block 1800s headless |
| Credential withholding across bash / hooks / plugins / MCP | provider key no longer inherited by any agent-spawned child |
| `Session.session_id` + `Session.system_prompt` | archives were all going to be `unknown_*`; the prompt was unrecoverable |
| `Usage.total_tokens()` double-counted cached tokens | 1850 → **1050** for the same usage |
| Configured `reasoning_effort` honoured (first-class field) | `high` + a short prompt silently sent `low` |
| `_collect_git_diff()` capped at 12k chars | system prompt 81,170 → **26,478** chars; **~13,700 tokens/request** |
| `ROLE_TOOLS` invariant test | caught `notebook_edit` (registered name is `edit_notebook_cell`) |
| Permission denial explains how to lift itself | shipped default denies bash; the message read as a defect |

Combined fixed overhead saved: **~14,900 tokens per request**. Test suite 759 → 789, no failures.

### The P0 is closed

`diagnose_agent.py` had only ever run against `mock-model`. It has now run for real (`mode: real`, `deepseek-chat`): **25/25 verdicts across 8 scenarios**, every one reporting `no redundant calls, no re-reads, no tool errors`, 2–5 iterations and 1–4 model calls per task, ~31s for the whole suite.

Two things follow. First, **loop discipline is not where the gap is** — that whole class of hypothesis is now excluded. Second, the run is cheap enough (~30s, a dozen calls) to serve as a regression gate rather than an annual ritual.

Caveats worth keeping in view: these are yucode's own scenarios, so it is a self-graded exam, and `deepseek-chat` is not the GPT-5.5 the company box runs.

### Measurement earned its keep

The git-diff cap is the case in point. The original review flagged it as structurally unbounded but measured only 2,351 chars on a clean tree — it looked like housekeeping. Measured with real work in the tree it was 66,794 chars, 82% of the system prompt, paid on every request and invalidating the cache prefix on every edit. Nothing about the code changed between those two readings; only the measurement did.

### Still open

- **`is_complex_prompt` may be a downgrade on a strong model.** Its keyword table (implement / build / create / analyze / 分析 / 調查) catches nearly any real development request, routing GPT-5.5 into a JSON planner, sequential workers, and a text-only judge that never inspects the workspace (`_validate()` calls with `tools=[]`). Suspected largest remaining loss; unmeasured. Measure `single` vs `auto` on one task set before changing the default. Note `classify_reasoning_effort()` shares the same helper, so the two move together.
- **Subagent results carry no stop reason.** Output truncated at `max_worker_steps` is indistinguishable from a completed result, so the parent builds on half-finished work as if it were fact. Cheapest correctness fix left.
- **No depth limit on the `agent` tool.** Without a `role` the child inherits the full tool set including `agent`, so recursion is unbounded. grok-build hard-limits depth to 1; dsh has `maxDepth`.
- **No stall detection in the validation retry loop** — see §3.
- **Compaction thresholds** (60k / 30k / 100k) are Qwen3-32B-era absolutes with no notion of the model's context window. grok-build uses 85% of the window, per-model overridable, with a token-budget retention policy that never splits a turn.
- **Tool output spill** — bash already writes full output to disk and returns a pointer (`shell.py:37-55`); no other tool does, so anything over the 32KB cap is lost outright.
- **`bash` description states none of its contract** — fresh shell per call, cmd.exe on Windows, safety-pattern refusals are policy rather than a retryable error, output already spilled to the returned path. The model learns these by hitting them. Note the reverse of the received wisdom: core tool descriptions are *too short*, not too defensive (`read_file` is 36 characters, shorter than dsh's).
- `v0.6.0` is still tagged locally only, now well behind `main`; `CLAUDE.md` is still deleted in the working tree, uncommitted.
