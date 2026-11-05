/**
 * The single place the version string lives.
 *
 * Deliberately not read from package.json: doing that needs either an import
 * attribute (which `erasableSyntaxOnly` tolerates but bundlers and Node's type
 * stripping disagree about) or a filesystem read relative to `import.meta.url`,
 * which then differs between running `src/**\/*.ts` directly and running the
 * compiled `dist/`. A constant is the thing that behaves identically in both.
 *
 * Keep it in step with `package.json`; `tests/smoke.test.ts` fails if it drifts.
 */
export const VERSION = "1.0.0-alpha.0";
