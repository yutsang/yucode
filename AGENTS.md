# Repository guidance

A TypeScript rewrite of a Python coding-agent harness. The Python is gone from
this tree and lives at the `python-final` tag; what replaced it is a plan, a
description of what the Python did, and the measured behaviour a port has to
reproduce.

Read in this order before writing anything:

1. `docs/REWRITE_PLAN.md` — what is being built and in what order. **Its §0.0
   corrections section wins over the plan body wherever they disagree**; 30
   claims in the body did not survive checking, and four of them would have
   changed the port.
2. `docs/ARCHITECTURE.md` — how the pieces fit and which contracts are frozen.
3. `docs/PYTHON_IMPLEMENTATION_SUMMARY.md` — what the Python did, with
   `file:line` into the tag. The tree it cites is unpacked, untracked, at
   `_legacy_python/`.
4. The goldens for whatever you are touching. They are the oracle, not
   documentation — assert against the file, never against the prose.

---

## Porting

**Port behaviour, not structure.** Where the Python was written against an
observed failure, the comment or the commit message usually says so; keep the
behaviour and its test. Where it was transliterated from claw-code and never
executed, don't port it at all — §A.7 of the plan lists what that covers.

**Every deviation is a decision, and it gets written down.** The goldens record
what the Python produced. When the port should differ — because the Python was
wrong, not because the port is easier that way — say so in the case's `note`,
in `manifest.json`, and in the commit message. Corrections 17 to 28 in the plan
are the deviations already identified.

**Evidence legend.** [M] measured · [V] verified in code · [R] read in a
reference repo · [A] assumed. Don't ship an [A].

**No dead code.** No speculative configurability. An abstraction with one
caller is not an abstraction.

**Keep the comparison conditions explicit.** A coordinator eval is not a
single-loop baseline. Before claiming parity, match orchestration mode,
configuration and fixture inputs; record any remaining difference.

**Review the checks themselves.** Prove a guard rejects the forbidden input,
not only that the current tree passes it. Native-file checks must include
package test/docs/example directories and executable headers without extensions.

---

## TypeScript

**Erasable syntax only.** The sources run under Node's type stripping without a
build step, so: no `enum` (use an `as const` object plus a union type), no
`namespace`, no parameter properties, no `import x = require()`. `tsconfig`
rejects these and eslint explains them.

**Relative imports carry the `.ts` extension.** `rewriteRelativeImportExtensions`
turns them into `.js` on the way into `dist/`, which is what makes the same
file runnable directly and compilable for deployment.

**`node:` prefix on every builtin.** `import process from "node:process"`, not
`"process"`.

**Entry-point guard is `import.meta.main`.** Comparing `import.meta.url` to
`process.argv[1]` is never equal on Windows — argv is `D:\a\...\main.js`,
the url is `file:///D:/a/.../main.js` — and it fails silently: the process
prints nothing and exits 0. Anything that only runs as a script needs a test
that spawns it, because calling the function directly cannot reach the guard.

**Types are imports too**: `import { type Foo, bar } from "./x.ts"`.

**The layering rule is a lint error, not a convention.** Nothing under
`src/runtime`, `src/tools`, `src/provider` or `src/session` may import from
`src/cli`, `src/tui`, `src/bridge`, `src/server` or `src/plugins`.

---

## Dependencies

**Pure JavaScript or WebAssembly. No exceptions.** Run
`npm run check:binaries` before every commit — it also runs as `pretest`. A new
runtime dependency needs a line in the commit message saying why, and
confirmation that its whole tree was checked.

Install is `npm ci --ignore-scripts`, on every machine and in CI. `.npmrc`
pins `save-exact` and `omit=optional`, and neither is decoration: a caret range
that resolves to a new minor with a native optional dependency is the second
way a binary arrives.

---

## Tests

`node --test`, `node:assert/strict`. No vitest.

A test's name should say what breaks, not what it calls. Prefer one test that
fails for one reason over a suite that fails for six.

Every work package runs on `windows-latest` as well as ubuntu. Windows is the
target, not a port: use `node:path`, normalise backslashes in anything the
model typed, never assume symlinks or `/bin/sh`.

---

## Commits and safety

Explain the **why** in the house style — read `git log` before `python-final`
for the register. No `Co-Authored-By` trailers, no "Generated with" footers.
One work package is one or a few commits.

**Check every diff for hostnames, keys, client or firm names before
committing.** Placeholders only: `api.workbench.example.internal`,
`x-tenant-*`, `FILL-ME`. This repository was public for eight weeks once and
had to be scrubbed and rewritten.

**Destructive git needs an explicit yes each time** — `rm -r`, force push, tag
deletion, orphan branches — auto-accept session or not.

Communicate with the owner in Traditional Chinese. Code, comments and commit
messages in English.
