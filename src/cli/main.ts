#!/usr/bin/env node
/**
 * The entry point both launchers resolve to.
 *
 * It does almost nothing yet on purpose: WP0 is the scaffold, and what this
 * file proves is that the toolchain works end to end — TypeScript compiles to
 * `dist/cli/main.js`, a relative `.ts` import is rewritten to `.js` on the way
 * out, and `yucode.cmd` / `yucode` reach the result. The agent itself starts
 * at WP1 (see docs/REWRITE_PLAN.md §5).
 */
import process from "node:process";

import { VERSION } from "../version.ts";

const NOT_YET = `yucode ${VERSION}

The TypeScript rewrite has not started. This tree carries the plan, the
description of the Python implementation it replaces, and the behavioural
baselines the port is measured against:

  docs/REWRITE_PLAN.md                  what to build, in what order
  docs/PYTHON_IMPLEMENTATION_SUMMARY.md what the Python did
  tests/golden/                         what it produced

Run \`python tools/golden/check_all.py\` to confirm the baselines still hold.`;

export function main(argv: readonly string[]): number {
  const args = argv.slice(2);
  if (args[0] === "version" || args[0] === "--version" || args[0] === "-v") {
    process.stdout.write(`${VERSION}\n`);
    return 0;
  }
  process.stdout.write(`${NOT_YET}\n`);
  // Non-zero would be wrong: nothing failed, there is simply nothing to run.
  return 0;
}

// True only when this file is the entry point, so importing `main()` from a
// test does not trigger it.
//
// The obvious comparison -- `import.meta.url === \`file://${process.argv[1]}\`` --
// is wrong on Windows and silently so: argv[1] is `D:\a\...\main.js` while
// import.meta.url is `file:///D:/a/.../main.js`, so the guard never fires, the
// process prints nothing and exits 0. It passed on Linux and macOS for a week.
if (import.meta.main) {
  process.exit(main(process.argv));
}
