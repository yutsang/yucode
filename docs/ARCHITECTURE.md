# Architecture

What this tree is being built into, and the handful of rules that decide where
a new file goes. `docs/REWRITE_PLAN.md` says what to build and in what order;
this says how it fits together. Where the two disagree, the plan's §0.0
corrections section wins over both.

---

## 1. One rule, and what it buys

**The loop knows nothing about how it is being driven.**

```
  UI clients   ┌──────────┬──────────┬────────────────┬──────────────────┐
               │ cli      │ tui      │ bridge (JSONL) │ server (HTTP+SSE)│
               └────┬─────┴──────────┴────────────────┴──────────────────┘
                    │  consumes an AgentEvent stream, calls runtime.run()
  stateful    ┌─────▼──────────────────────────────────────────────────┐
  core        │ runtime/runtime.ts — composes config, provider, tools, │
              │ session store, metrics, audit, background tasks; owns  │
              │ lifecycle and everything that touches disk             │
              └─────┬──────────────────────────────────────────────────┘
                    │
  stateless   ┌─────▼──────────────────────────────────────────────────┐
  loop        │ runtime/agent-loop.ts — runTurn(): prompt assembly →   │
              │ provider call → tool dispatch → supervisors →          │
              │ reminders → yields AgentEvent. Touches no filesystem   │
              │ except through an injected SessionLog / ToolContext    │
              └─────┬──────────────────────────────────────────────────┘
                    │
   provider/  tools/  security/  hooks/  mcp/  prompt/  compaction/  session/
```

Four consumers drive the same loop: the CLI, the Ink TUI, the VS Code bridge
and the HTTP server. If the loop reached back into any of them, adding the
fifth would mean changing the fourth, and a test would have to stand up an
interface to exercise a tool.

It is not self-enforcing. `eslint.config.js` bans imports from `cli`, `tui`,
`bridge`, `server` and `plugins` inside `src/runtime`, `src/tools`,
`src/provider` and `src/session`. One convenience import is all it takes.

The Python this replaces had the loop, the renderer and the CLI in one call
graph, which is why its turn loop could not be tested without a terminal.

---

## 2. Layout

One npm package at the repo root. Not a monorepo: the boundary is enforced by
lint, and a split into `packages/*` can happen if a second consumer ever needs
to install the core on its own.

```
package.json        type: module, engines.node >=24, no "bin" (see yucode.cmd)
tsconfig.json       NodeNext, ES2023, strict, erasableSyntaxOnly; checks src
                    and tests together and emits nothing
tsconfig.build.json narrows to src so rootDir can be src, which is what puts
                    the entry point at dist/cli/main.js
.npmrc              ignore-scripts, save-exact, omit=optional
yucode.cmd          @node "%~dp0dist\cli\main.js" %*      ← the only launcher
yucode              the POSIX equivalent
config/default.yml  bundled defaults, same keys as the Python's config.yml

src/
  config/      schema, discovery + deep-merge + env, trust gate, state dir
  provider/    one OpenAI-compatible provider, its SSE parser, retry ladder,
               circuit breaker, 400 self-heal, and the recording/mock/logging
               decorators the tests drive
  session/     append-only event log (JSONL), the projection that turns it
               into a provider message list, the store, the token estimator
  tools/       the Tool contract, the registry, the workspace path boundary,
               then fs/ patch/ shell/ web/ misc/ memory/ agent/ office/
  security/    permission modes and rules, command safety, redaction,
               child-process environment scrubbing
  hooks/       the five hook events and their exit-code protocol
  mcp/         stdio client, server manager, presets, and `mcp-serve`
  prompt/      the assembler and its sections, instruction-file discovery,
               skills, git context, memory index
  compaction/  when to compact, the heuristic summary, the LLM summary
  runtime/     the loop, the stateful runtime, supervisors, reminders
  observability/ audit log, metrics, retention sweep
  cli/ tui/ bridge/ server/ plugins/   the four consumers, plus plugin hosting
  diagnose/ eval/                      the two harnesses that measure it

tests/         mirrors src/; *.test.ts under node --test
               golden/ — the baselines captured from the Python (see §5)
tools/golden/  the scripts that captured them
docs/legacy/   the 2026-07/08 analysis documents
_legacy_python/ UNTRACKED — the Python tree, unpacked from the python-final tag
```

---

## 3. The three core contracts

Written first; everything else is filled in behind them.

**A tool** is a name, a description that states its contract, a zod schema, a
permission class, and an `execute` that receives a `ToolContext`. The context
is the only way it reaches the outside: `resolvePath` for the workspace
boundary, `spill` for oversized output, `events` to emit, `signal` to cancel.
A tool that reaches around it is a tool that cannot be tested.

**A provider** is one method. `complete()` takes messages, tool schemas and a
signal, and returns text, tool calls, usage, reasoning and a finish reason.
There is no provider registry and no SDK: two gateways are supported — DeepSeek
through an OpenAI-compatible endpoint, and an Azure/APIM-style deployment URL —
and the value is in the quirks (see `PYTHON_IMPLEMENTATION_SUMMARY.md` §9), not
in the abstraction.

**A session** is an append-only log of events, and the message list sent to the
model is a projection over it. This is the one structural change the rewrite
makes deliberately. The Python used a single `messages` list as storage, model
input and audit record at once, and the 2026-08 audit traced five defects to
exactly that: the system prompt was never recorded, compaction destroyed
history and needed an archive to work around itself, supervisor injections were
indistinguishable from things the user said, reasoning was dropped, and
checkpoints were written but never read. A projection over a log removes all
five and costs about 150 lines.

---

## 4. What must not change

These are contracts with software that already exists — the operator's
`~/.yucode/settings.yml`, the VS Code extension, the two production skills. A
port that gets the behaviour right and the key names wrong has not shipped.

- **Config keys, discovery order and merge semantics.** Lists concatenate,
  dicts recurse, an empty overlay value never shadows a non-empty base value.
  Unknown keys warn, never fail.
- **The state directory**, including the hash: `sha256` of the resolved
  workspace path string, first 12 hex characters. Get this wrong and every
  saved session becomes unreachable.
- **Tool names and their JSON shapes.** Error envelopes stay
  `{"error": …}`; MCP tools stay `mcp__<server>__<tool>`.
- **The event vocabulary**, which is the wire format for `--json`, the bridge,
  the server and the TUI at once.
- **The hook protocol**: environment variables, JSON on stdin, exit 0 allow /
  2 deny / anything else warn.
- **Headless output and exit codes** for `yucode chat --json`.

Each is pinned by a file under `tests/golden/harness/`. Assert against the
file, not against the prose.

---

## 5. How correctness is decided

`tests/golden/` is the Python's measured behaviour, captured before it was
deleted. It is not documentation; it is the oracle.

- `harness/` — 17 files: every tool's JSON Schema in registration order, three
  resolved config dictionaries, the rendered system prompt with and without git
  context, the session-to-provider projection, twenty deterministic functions
  input-to-output, and both behavioural diagnostics at 25/25.
- `office/` — 103 cases covering the 22 domain tools, with the fixtures they
  run against and a manifest recording library versions and per-glyph font
  measurements.
- `fonts/` — two OFL subsets, so the text-metric cases can be checked on a
  machine that has neither Arial nor a CJK font.

`python tools/golden/check_all.py` re-runs all three captures and diffs them.
It needs `_legacy_python/`, which is unpacked from the `python-final` tag.

Two things the goldens say that are easy to disbelieve. Python's `str()` for
floats is not JavaScript's `String()` — `1e-7` is `"1e-07"` and `1e16` is
`"1e+16"` — and every `round()` in the office layer is banker's rounding on the
binary double, not `Math.round`. Both are recorded as the Python produced them,
deliberately un-normalised, because normalising either would hide the exact
failure the oracle exists to catch.

---

## 6. The dependency rule

Every dependency must be pure JavaScript or WebAssembly. This is not taste.
The target is a locked-down corporate Windows box with no admin rights, where
endpoint protection quarantines any freshly created unsigned `.exe` or `.node`,
so one transitive package with a `binding.gyp` turns a deployment into an
incident.

`scripts/check-no-binaries.mjs` walks `node_modules` and fails on a native
artefact (by extension or executable header), a build file, or a manifest
that declares one — including the
platform-scoped `optionalDependencies` pattern that ships prebuilt binaries
without compiling anything. It scans test/docs/example directories and linked
packages too, and prevents symlink cycles through canonical directory paths.
It checks unpacked files; compressed archives are not inspected. It runs as
`pretest` and as a CI step.

Two consequences worth knowing before reaching for something:

- **TypeScript is pinned below 7.** TypeScript 7 is the native compiler, and it
  lists twenty platform packages in `dependencies` as well as
  `optionalDependencies` — so `omit=optional` does not help and the check would
  fail on a plain install.
- **No esbuild, vitest, tsx, bun, biome, oxlint, `@vscode/ripgrep`, keytar,
  node-pty, or better-sqlite3.** The toolchain is `tsc` plus `node --test`.

The deployment artefact is a zip of `dist/`, `node_modules/`, `config/`,
`.claw/` and the launcher, built and scanned here. Because nothing is native,
the same `node_modules` works on Windows and macOS.

---

## 7. Windows is the target, not a port

Everything in `PYTHON_IMPLEMENTATION_SUMMARY.md` §17 is a failure that was
observed on the real box. The two that catch people:

- `cmd.exe` mangles any command containing more than two double quotes. Node's
  `spawn(cmd, { shell: true })` has to be verified there, not assumed — a
  command quoting both an executable path and an argument is the failing shape.
- There is no ripgrep. `grep` and `glob` use a directory walk with artifact
  pruning, and use `rg` only if it happens to be on PATH.

Use `node:path` everywhere, normalise backslashes in anything the model typed,
never assume symlinks, never assume `/bin/sh`. Every work package's tests run
on the `windows-latest` runner as well as ubuntu.
