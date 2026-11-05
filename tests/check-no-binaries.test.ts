import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const scanner = fileURLToPath(new URL("../scripts/check-no-binaries.mjs", import.meta.url));

function scan(files: Record<string, string | Uint8Array>, linked: false | "cycle" | "broken" = false) {
  const root = mkdtempSync(join(tmpdir(), "yucode-binary-test-"));
  try {
    for (const [name, content] of Object.entries(files)) {
      const path = join(root, "node_modules", "fixture", name);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, content);
    }
    if (linked) {
      const target = join(root, "linked-package");
      mkdirSync(target);
      writeFileSync(join(target, "payload"), Buffer.from("7f454c46", "hex"));
      symlinkSync(target, join(root, "node_modules", "linked"), "junction");
      if (linked === "cycle") symlinkSync(target, join(target, "cycle"), "junction");
      else rmSync(target, { recursive: true });
    }
    const result = spawnSync(process.execPath, [scanner, "--json"], { cwd: root, encoding: "utf8", timeout: 10_000 });
    assert.equal(result.error, undefined);
    assert.ok(result.status === 0 || result.status === 1, result.stderr);
    const report = JSON.parse(result.stdout) as {
      ok: boolean;
      findings: { path: string }[];
      wasm: string[];
    };
    return { status: result.status, ...report };
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("native files cannot hide in test, documentation or example directories", () => {
  for (const directory of [".git", ".bin", "test", "tests", "__tests__", "docs", "example", "examples"]) {
    const result = scan({ [`${directory}/payload.exe`]: "fixture" });
    assert.equal(result.status, 1, directory);
    assert.equal(result.findings[0]?.path, `node_modules/fixture/${directory}/payload.exe`);
  }
});

test("native headers are rejected without relying on the filename", () => {
  for (const hex of ["4d5a", "7f454c46", "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca", "213c617263683e0a"]) {
    for (const name of ["payload", "payload.txt", "payload.wasm"]) {
      assert.equal(scan({ [name]: Buffer.from(hex, "hex") }).status, 1, `${hex}: ${name}`);
    }
  }
});

test("JavaScript, JSON and WebAssembly remain allowed", () => {
  const result = scan({
    "index.js": "export default 1;",
    "package.json": JSON.stringify({ name: "fixture", version: "1.0.0" }),
    "layout.wasm": Buffer.from("0061736d01000000", "hex"),
  });
  assert.equal(result.status, 0);
  assert.equal(result.ok, true);
  assert.deepEqual(result.wasm, ["node_modules/fixture/layout.wasm"]);
});


test("linked packages are scanned without looping through directory cycles", () => {
  const result = scan({ "index.js": "export default 1;" }, "cycle");
  assert.equal(result.status, 1);
  assert.ok(result.findings.some((finding) => finding.path.endsWith("/payload")));
});

test("malformed package manifests cannot produce a successful audit", () => {
  for (const content of ["{", "null", "[]"]) {
    assert.equal(scan({ "package.json": content }).status, 1, content);
  }
});

test("native build declarations are rejected even without an installed binary", () => {
  for (const fields of [
    { gypfile: true },
    { binary: { module_name: "fixture" } },
    { dependencies: { "node-gyp": "1.0.0" } },
    { optionalDependencies: { "fixture-win32-x64": "1.0.0" } },
    { os: ["win32"] },
  ]) {
    assert.equal(scan({ "package.json": JSON.stringify(fields) }).status, 1);
  }
  assert.equal(scan({ "docs/binding.gyp": "{}" }).status, 1);
});


test("broken package links cannot produce a successful audit", () => {
  const result = scan({ "index.js": "export default 1;" }, "broken");
  assert.equal(result.status, 1);
  assert.ok(result.findings.some((finding) => finding.path === "node_modules/linked"));
});
