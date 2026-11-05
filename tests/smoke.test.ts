/**
 * WP0's acceptance test: the toolchain runs, and the oracle is intact.
 *
 * The first half proves `node --test` can execute TypeScript with a relative
 * `.ts` import — which is the whole bet of the tsconfig (`erasableSyntaxOnly`
 * plus `rewriteRelativeImportExtensions`), and the thing most likely to be
 * wrong on a machine with the wrong Node.
 *
 * The second half is the check the CI placeholder used to do inline. It is
 * cheap and it guards the only content this tree currently has: if a golden
 * stops parsing, or the fonts go missing, every later work package is
 * measuring against nothing.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { test } from "node:test";

import { VERSION } from "../src/version.ts";
import { main } from "../src/cli/main.ts";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const GOLDEN = join(REPO_ROOT, "tests", "golden");

function readJson(...parts: string[]): unknown {
  return JSON.parse(readFileSync(join(...parts), "utf8"));
}

test("the version constant matches package.json", () => {
  const pkg = readJson(REPO_ROOT, "package.json") as { version: string };
  assert.equal(VERSION, pkg.version);
});

test("main() returns 0 and writes something", () => {
  const written: string[] = [];
  const original = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk: string) => {
    written.push(String(chunk));
    return true;
  };
  try {
    assert.equal(main(["node", "main.ts"]), 0);
    assert.equal(main(["node", "main.ts", "version"]), 0);
  } finally {
    process.stdout.write = original;
  }
  assert.ok(written.join("").includes(VERSION));
});

test("the entry point runs as a script", () => {
  // Calling main() directly cannot exercise the `import.meta.main` guard, and
  // getting that guard wrong fails silently: the process prints nothing and
  // exits 0. A first version compared import.meta.url to argv[1], which is
  // never equal on Windows, and only the CI job on windows-latest caught it.
  // Running the source as a subprocess catches it wherever `npm test` runs.
  const output = execFileSync(process.execPath, [join(REPO_ROOT, "src", "cli", "main.ts"), "version"], {
    encoding: "utf8",
  });
  assert.equal(output.trim(), VERSION);
});

test("every golden parses", () => {
  const files = [
    join(GOLDEN, "office", "cases.json"),
    join(GOLDEN, "office", "manifest.json"),
    ...readdirSync(join(GOLDEN, "harness"))
      .filter((name) => name.endsWith(".json"))
      .map((name) => join(GOLDEN, "harness", name)),
  ];
  assert.ok(files.length >= 12, `expected the harness baselines, found ${files.length} json files`);
  for (const file of files) {
    assert.doesNotThrow(() => JSON.parse(readFileSync(file, "utf8")), `${file} does not parse`);
  }
});

test("the office oracle agrees with itself", () => {
  const cases = readJson(GOLDEN, "office", "cases.json") as {
    case_count: number;
    cases: { id: string; outcome: string }[];
  };
  assert.equal(cases.case_count, cases.cases.length);
  assert.equal(new Set(cases.cases.map((c) => c.id)).size, cases.cases.length, "duplicate case id");
  for (const one of cases.cases) {
    assert.ok(["ok", "error", "skipped"].includes(one.outcome), `${one.id}: ${one.outcome}`);
  }
});

test("the fixtures and the vendored fonts are present", () => {
  const fixtures = readdirSync(join(GOLDEN, "office", "fixtures"));
  assert.ok(fixtures.length >= 10, `only ${fixtures.length} fixtures`);
  const fonts = readdirSync(join(GOLDEN, "fonts"));
  assert.equal(fonts.filter((name) => name.endsWith(".ttf")).length, 2);
  // The licence texts are a condition of shipping the subsets, not a nicety.
  assert.ok(fonts.includes("OFL-NotoSans.txt"));
  assert.ok(fonts.includes("OFL-NotoSansSC.txt"));
  assert.ok(fonts.includes("SOURCES.json"));
});

test("no golden carries a path from the machine that captured it", () => {
  // Every one of these was a real finding: a temp directory in a diagnostic
  // trace, the operator's home in a prompt, the capture host's OS in a hash.
  //
  // `example` is exempt because state_layout.json pins the workspace-key hash
  // of four literal paths, two of which are home directories, and the whole
  // point of those entries is that the string never changes. The repo's
  // placeholder convention is `example` throughout — see AGENTS.md.
  const forbidden = [
    /\/var\/folders\//,
    /\/Users\/(?!example\b)[a-z]/i,
    /C:\\Users\\(?!example\b)/i,
    /Darwin \d/,
  ];
  for (const area of ["office", "harness"] as const) {
    for (const name of readdirSync(join(GOLDEN, area))) {
      if (!name.endsWith(".json") && !name.endsWith(".txt")) continue;
      const text = readFileSync(join(GOLDEN, area, name), "utf8");
      for (const pattern of forbidden) {
        assert.ok(!pattern.test(text), `${area}/${name} leaks ${pattern}`);
      }
    }
  }
});
