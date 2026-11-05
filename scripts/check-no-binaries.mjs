#!/usr/bin/env node
// Fail if anything in node_modules is, or would build, a native binary.
//
// The target machine is a locked-down corporate Windows box where endpoint
// protection flags any freshly created unsigned .exe or .node. That is not a
// preference about dependencies; it is the reason this project can run there
// at all. A single transitive package with a binding.gyp turns `npm ci` into
// an installation that gets quarantined, so the rule is mechanised rather
// than written down.
//
// WebAssembly is reported and allowed. A .wasm file is not a PE binary and is
// loaded by Node itself; Ink's yoga layout ships one. Whether the box's EDR
// agrees has to be confirmed on the box (docs/REWRITE_PLAN.md §9 Q1).
//
// Run: node scripts/check-no-binaries.mjs [--json]

import { Buffer } from "node:buffer";
import { closeSync, openSync, readSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const MODULES = join(ROOT, "node_modules");

/** Extensions that are a native binary, whatever the platform. */
const BINARY_EXTENSIONS = new Set([".exe", ".dll", ".node", ".so", ".dylib", ".a", ".lib"]);

/** Files whose presence means a package intends to compile something. */
const BUILD_FILES = new Set(["binding.gyp", "binding.gyp.in", "CMakeLists.txt"]);

/** package.json fields that pull a prebuilt binary or run node-gyp. */
const NATIVE_FIELDS = ["gypfile", "binary"];
const NATIVE_DEPENDENCIES = [
  "prebuild-install",
  "node-gyp-build",
  "node-gyp",
  "node-pre-gyp",
  "@mapbox/node-pre-gyp",
  "nan",
  "node-addon-api",
];

// Inspect headers even when an executable has no extension or a misleading one.
const NATIVE_MAGIC = new Set([
  "7f454c46", // ELF
  "feedface", "cefaedfe", "feedfacf", "cffaedfe", // Mach-O
  "cafebabe", "bebafeca", "cafebabf", "bfbafeca", // universal Mach-O
]);

function hasNativeHeader(path) {
  const header = Buffer.alloc(8);
  const fd = openSync(path, "r");
  let length;
  try {
    length = readSync(fd, header, 0, header.length, 0);
  } finally {
    closeSync(fd);
  }
  return (length >= 2 && header.subarray(0, 2).toString("ascii") === "MZ") ||
    (length >= 4 && NATIVE_MAGIC.has(header.subarray(0, 4).toString("hex"))) ||
    (length === 8 && header.toString("ascii") === "!<arch>\n");
}

const findings = [];
const wasm = [];

function record(kind, path, detail) {
  findings.push({ kind, path: relative(ROOT, path).split(sep).join("/"), detail });
}

/** The package a path belongs to, for a message that names something installable. */
function owningPackage(path) {
  const parts = relative(MODULES, path).split(sep);
  if (parts[0]?.startsWith("@")) return parts.slice(0, 2).join("/");
  return parts[0] ?? "?";
}

function checkManifest(path) {
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    record("invalid-manifest", path, String(error));
    return;
  }
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    record("invalid-manifest", path, "package.json must contain an object");
    return;
  }
  for (const field of NATIVE_FIELDS) {
    if (manifest[field]) record("native-field", path, `package.json sets "${field}"`);
  }
  for (const section of ["dependencies", "devDependencies", "optionalDependencies"]) {
    for (const name of Object.keys(manifest[section] ?? {})) {
      if (NATIVE_DEPENDENCIES.includes(name)) {
        record("native-dependency", path, `${section} includes "${name}"`);
      }
    }
  }
  // A package that resolves a different artefact per platform is shipping
  // prebuilt binaries even when none is installed on this machine.
  const optional = Object.keys(manifest.optionalDependencies ?? {});
  const platformScoped = optional.filter((name) =>
    /(darwin|linux|win32|android|freebsd)[-_]?(arm64|x64|ia32|arm)?/.test(name),
  );
  if (platformScoped.length > 0) {
    record("platform-optional", path, `optionalDependencies: ${platformScoped.join(", ")}`);
  }
  if (manifest.os || manifest.cpu) {
    record("platform-locked", path, `os=${manifest.os ?? "-"} cpu=${manifest.cpu ?? "-"}`);
  }
}

const visitedDirectories = new Set();

function walk(directory) {
  const canonical = realpathSync(directory);
  if (visitedDirectories.has(canonical)) return;
  visitedDirectories.add(canonical);
  const entries = readdirSync(directory, { withFileTypes: true });
  for (const entry of entries) {
    const path = join(directory, entry.name);
    let target = entry;
    if (entry.isSymbolicLink()) {
      try {
        target = statSync(path);
      } catch (error) {
        record("unreadable-link", path, String(error));
        continue;
      }
    }
    if (target.isDirectory()) {
      walk(path);
      continue;
    }
    const dot = entry.name.lastIndexOf(".");
    const extension = dot === -1 ? "" : entry.name.slice(dot).toLowerCase();
    if (BINARY_EXTENSIONS.has(extension) || hasNativeHeader(path)) {
      record("binary", path, `${owningPackage(path)} ships ${entry.name}`);
    } else if (extension === ".wasm") {
      wasm.push(relative(ROOT, path).split(sep).join("/"));
    } else if (BUILD_FILES.has(entry.name)) {
      record("build-file", path, `${owningPackage(path)} would compile`);
    } else if (entry.name === "package.json") {
      checkManifest(path);
    }
  }
}

function main() {
  const asJson = process.argv.includes("--json");
  let installed = false;
  try {
    installed = statSync(MODULES).isDirectory();
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    // Absent, which is the normal state of a fresh checkout.
  }

  if (!installed) {
    // Not an error: the scaffold is checked before anything is installed, and
    // CI runs this as `pretest` on a fresh checkout too.
    const message = "node_modules is absent; nothing to check";
    if (asJson) console.log(JSON.stringify({ ok: true, skipped: true, message }));
    else console.log(`[skip] ${message}`);
    return 0;
  }

  walk(MODULES);

  if (asJson) {
    console.log(JSON.stringify({ ok: findings.length === 0, findings, wasm }, null, 2));
  } else {
    for (const file of wasm) console.log(`[wasm] ${file}`);
    for (const finding of findings) console.log(`[FAIL] ${finding.kind}: ${finding.path} — ${finding.detail}`);
    if (findings.length === 0) {
      console.log(`[ok] no native binaries in node_modules${wasm.length ? ` (${wasm.length} wasm allowed)` : ""}`);
    } else {
      const packages = [...new Set(findings.map((f) => owningPackage(join(ROOT, f.path))))];
      console.log(`\n${findings.length} finding(s) across: ${packages.join(", ")}`);
      console.log("Every dependency must be pure JS or WASM — see docs/REWRITE_PLAN.md §3.6.");
    }
  }
  return findings.length === 0 ? 0 : 1;
}

process.exit(main());
