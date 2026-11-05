// Flat config. Two rules here do real work; the rest is the recommended set.
//
// The first is the layering rule from docs/REWRITE_PLAN.md §3.1. The whole
// point of the architecture is that the turn loop, the tools and the provider
// know nothing about how they are being driven, so that the same loop serves
// the CLI, the TUI, the VS Code bridge and the HTTP server, and so that a test
// can drive it with no interface at all. That property is not self-enforcing:
// one `import { render } from "../cli/render.ts"` inside a tool quietly ends
// it, and nothing else in the build would complain.
//
// The second is `erasableSyntaxOnly`'s companion. tsconfig rejects enums,
// namespaces, parameter properties and `import x = require()` at compile time,
// but a developer running `node --test` on the sources directly gets a
// confusing runtime error instead of a type error, so the lint says it plainly.

import js from "@eslint/js";
import tseslint from "typescript-eslint";

/** Layers that exist to consume the core, and must never be consumed by it. */
const INTERFACE_LAYERS = ["cli", "tui", "bridge", "server", "plugins"];

const interfaceImportBan = INTERFACE_LAYERS.flatMap((layer) => [
  `**/${layer}/**`,
  `../${layer}/*`,
  `../../${layer}/*`,
  `../../../${layer}/*`,
]);

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "_legacy_python/**", "tests/golden/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Erasable TypeScript only, so the sources run under Node's type
      // stripping without a build step.
      "@typescript-eslint/no-namespace": "error",
      "@typescript-eslint/parameter-properties": ["error", { prefer: "class-property" }],
      "no-restricted-syntax": [
        "error",
        {
          selector: "TSEnumDeclaration",
          message:
            "enum is not erasable syntax. Use an `as const` object plus a union type.",
        },
        {
          selector: "TSImportEquals",
          message: "`import x = require()` is not erasable syntax. Use an ESM import.",
        },
      ],
      // An unused promise in a tool or the loop is a dropped error. node:test's
      // `test()` returns one that callers are not meant to await, so it is
      // named as safe rather than the rule being switched off in tests --
      // a dropped promise inside a test body is still a bug.
      "@typescript-eslint/no-floating-promises": [
        "error",
        {
          allowForKnownSafeCalls: [
            { from: "package", package: "node:test", name: ["test", "it", "describe", "suite"] },
          ],
        },
      ],
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { fixStyle: "inline-type-imports" },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      eqeqeq: ["error", "always", { null: "ignore" }],
      "no-console": "off",
    },
  },
  {
    // The core: everything a headless run needs, and nothing that renders.
    files: ["src/runtime/**/*.ts", "src/tools/**/*.ts", "src/provider/**/*.ts", "src/session/**/*.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: interfaceImportBan,
              message:
                "The loop, the tools and the provider must not import an interface layer. " +
                "They emit AgentEvents; the interface decides what to do with them. " +
                "See docs/ARCHITECTURE.md.",
            },
          ],
        },
      ],
    },
  },
  {
    files: ["tests/**/*.ts"],
    rules: {
      // A test that asserts on a rejected promise reads better with the
      // unbound method than without it.
      "@typescript-eslint/unbound-method": "off",
    },
  },
  {
    files: ["scripts/**/*.mjs", "*.js"],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: {
      // Spread theirs first. disableTypeChecked works by setting
      // parserOptions.projectService to false, and a bare `languageOptions`
      // here replaces that wholesale -- which is how these two plain-JS files
      // ended up being asked for a tsconfig project they are not in.
      ...tseslint.configs.disableTypeChecked.languageOptions,
      globals: {
        // Node's ambient globals. Declared rather than pulled from the
        // `globals` package: four names do not justify a dependency in a tree
        // whose whole dependency policy is "justify every one".
        console: "readonly",
        process: "readonly",
        URL: "readonly",
        fetch: "readonly",
      },
    },
  },
);
