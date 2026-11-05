#!/usr/bin/env python3
"""Capture the office tools' behaviour as an oracle for the TypeScript port.

Runs every case in `cases.py` through `ToolRegistry.execute` against
deterministic synthetic fixtures, and writes:

    tests/golden/office/fixtures/      the inputs, committed as binaries
    tests/golden/office/cases.json     what each tool returned, normalised
    tests/golden/office/manifest.json  versions, font measurements, notes

Why `ToolRegistry.execute` and not `AgentRuntime._execute_tool`: the registry
is the tool, and the runtime adds permission checks, hooks, secret redaction
and a 32 KB cap — none of which the office port is reproducing. Handler
exceptions propagate uncaught there, which is what makes the error cases
capturable at all.

Subcommands:
    capture        (default) rebuild everything
    check          rebuild into a temp dir and diff; exit 1 on drift
    vendor-fonts   subset and rename the two OFL fonts

Exit codes: 0 ok · 1 drift · 2 environment · 3 capture failure.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
# The Python package these scripts measure was removed from the tree at the
# reset; it lives on, untracked, in the backup unpacked from the python-final
# tag. Keeping the scripts here rather than only in that backup is deliberate:
# they are the recipe for every file under tests/golden/, and a recipe you
# cannot find is a recipe nobody re-runs.
_LEGACY = REPO_ROOT / "_legacy_python"
if (_LEGACY / "coding_agent").is_dir():
    sys.path.insert(0, str(_LEGACY))
DEFAULT_OUT = REPO_ROOT / "tests" / "golden" / "office"

# settings.py snapshots YUCODE_HOME at import time (settings.py:58), and
# tests/conftest.py's isolation fixture is a pytest fixture that does not run
# here — so this has to happen before the coding_agent imports below.
_SCRATCH_HOME = Path(tempfile.mkdtemp(prefix="yucode-golden-home-"))
os.environ["YUCODE_HOME"] = str(_SCRATCH_HOME)

sys.path.insert(0, str(REPO_ROOT))

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions  # noqa: E402
from coding_agent.tools import ToolRegistry  # noqa: E402

from tools.golden import cases as case_table  # noqa: E402
from tools.golden import fixtures as fx  # noqa: E402
from tools.golden import fonts as font_tools  # noqa: E402
from tools.golden import projections  # noqa: E402

WS_TOKEN = "<WS>"
TMP_TOKEN = "<TMP>"

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Normalizer:
    """Machine-specific paths out, tokens in.

    Two tokens, not one. `_resolve_path` reports both the resolved and the
    canonical path when something escapes the workspace, and the escaping path
    is by definition outside it — so the workspace is `<tmp>/ws` and its parent
    is tokenised too. Both are matched against the RESOLVED root, because on
    macOS `/var` canonicalises to `/private/var` and an unresolved prefix would
    silently miss.

    Each needle is tried in three spellings, and this matters more than it
    looks. A JSON-serialised tool result writes every Windows backslash
    doubled, so `C:\\Users\\alice\\...` appears in the raw string as
    `C:\\\\Users\\\\alice\\\\...` and a needle with single backslashes matches
    nothing at all — leaving the operator's username in `scrubbed_sha256` for
    every case whose output embeds a path.

    The separator normalisation is per replacement rather than over the whole
    string. Doing it globally, gated on a token being present somewhere, was
    both inconsistent (the same text normalised in one case and not another)
    and corrupting (it rewrote `\\n` to `/n` inside any JSON that also named
    the workspace).
    """

    tokens: tuple[tuple[str, str], ...]

    def scrub(self, text: str) -> str:
        for needle, token in sorted(self.tokens, key=lambda pair: -len(pair[0])):
            for variant in (
                needle,
                needle.replace("\\", "/"),
                needle.replace("\\", "\\\\"),
            ):
                text = text.replace(variant, token)
        # Separators *after* a token: a Windows capture writes `<WS>\out\x.pptx`
        # (or `<WS>\\out\\x.pptx` once JSON-escaped) where this one writes
        # `<WS>/out/x.pptx`.
        for token in (WS_TOKEN, TMP_TOKEN):
            text = re.sub(
                re.escape(token) + r"((?:\\{1,2}[^\s\"'\\]+)+)",
                lambda m: token + m.group(1).replace("\\\\", "/").replace("\\", "/"),
                text,
            )
        return text

    def scrub_tree(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self.scrub_tree(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_tree(v) for v in value]
        return value

def _normalizer(workspace: Path, tmp_root: Path) -> Normalizer:
    return Normalizer((
        (str(workspace.resolve()), WS_TOKEN),
        (str(workspace), WS_TOKEN),
        (str(tmp_root.resolve()), TMP_TOKEN),
        (str(tmp_root), TMP_TOKEN),
    ))


# --------------------------------------------------------------------------
# running one case
# --------------------------------------------------------------------------


def _config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(
            base_url="https://provider.example.invalid",
            api_key="unused",
            model="fixture-model",
        ),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


def run_case(case: case_table.Case, fixture_bytes: dict[str, bytes],
             binding: font_tools.FontBinding | None) -> dict[str, Any]:
    tmp_root = Path(tempfile.mkdtemp(prefix="yucode-golden-case-"))
    try:
        workspace = tmp_root / "ws"
        workspace.mkdir()
        for name in case.files:
            (workspace / name).write_bytes(fixture_bytes[name])
        # A copy one level up, so the workspace-escape case has something real
        # to point at rather than failing on ENOENT before the boundary check.
        if any(arg == "../img.png" for arg in case.args.values()):
            (tmp_root / "img.png").write_bytes(fixture_bytes["img.png"])

        norm = _normalizer(workspace, tmp_root)
        registry = ToolRegistry(workspace, _config())

        record: dict[str, Any] = {
            "id": case.id,
            "tool": case.tool,
            "args": case.args,
            "workspace": {"files": list(case.files),
                          "pre_steps": [list(step) for step in case.pre_steps]},
        }

        context = font_tools.font_binding(binding) if binding else _null_context()
        with context:
            for pre_tool, pre_args in case.pre_steps:
                registry.execute(pre_tool, dict(pre_args))
            try:
                raw = registry.execute(case.tool, dict(case.args))
                outcome = "ok"
                error = None
            except Exception as exc:  # noqa: BLE001 - capturing is the job
                raw = None
                outcome = "error"
                error = _error_envelope(exc, norm)

        if outcome != case.expect:
            detail = error["message"] if error else (raw or "")[:2000]
            raise SystemExit(
                f"case {case.id!r} expected {case.expect} but got {outcome}: {detail}"
            )

        record["outcome"] = outcome
        record["error"] = error
        record["normalizations"] = []

        if outcome == "ok":
            record["output"] = _output_block(case, raw, norm, record["normalizations"])
        else:
            record["output"] = None
            record["normalizations"].append("error.envelope")

        if case.projection is not None and outcome == "ok":
            spec = case.projection
            target = workspace / spec.of
            value = projections.PROJECTORS[spec.extractor](target, **spec.params)
            record["projection"] = {
                "extractor": spec.extractor,
                "of": spec.of,
                "params": spec.params,
                "value": norm.scrub_tree(value),
            }
        else:
            record["projection"] = None

        record["font"] = _font_block(binding) if case.font_variant else None
        record["note"] = case.note
        return record
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _output_block(case: case_table.Case, raw: str, norm: Normalizer,
                  applied: list[str]) -> dict[str, Any]:
    import hashlib

    # Hash the SCRUBBED string, not the raw one. Five tools embed an absolute
    # path in their return value, and that path is a fresh temp directory on
    # every run — hashing the raw text made the golden churn for no reason.
    scrubbed = norm.scrub(raw)
    block: dict[str, Any] = {
        "kind": case.kind,
        "scrubbed_sha256": hashlib.sha256(scrubbed.encode("utf-8")).hexdigest(),
        "scrubbed_length": len(scrubbed),
        # Handlers disagree about serialisation — compact here, indent=2 there,
        # ensure_ascii off in one place. None of that is contract, but a port
        # that wants byte parity can check it against these two fields.
        "ascii_only": scrubbed.isascii(),
    }
    if case.kind == "json":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"case {case.id!r} is declared json but did not parse ({exc}): "
                f"{raw[:2000]}"
            )
        applied.extend(["json.parse", "path.workspace_token", "path.posix"])
        block["value"] = norm.scrub_tree(parsed)
    else:
        applied.extend(["path.workspace_token", "path.posix"])
        block["value"] = scrubbed
    return block


def _error_envelope(exc: Exception, norm: Normalizer) -> dict[str, Any]:
    message = norm.scrub(str(exc))
    envelope: dict[str, Any] = {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": message,
    }
    # Two handlers interpolate a Python repr(list) into their message --
    # _find_shape_by_name (office.py:812-813) and image_read's suffix
    # allow-list (office.py:1171). No TypeScript port should be asked to
    # reproduce ['a', 'b'] with single quotes, so the list is pulled out into
    # `parts` and the assertion can be structural, with `message` left
    # informational.
    for marker, key in (("Available shapes:", "available_shapes"),
                        ("expected one of", "expected_suffixes")):
        if marker not in message:
            continue
        _, _, tail = message.partition(marker)
        envelope["parts"] = {
            key: [
                item.strip().strip("'\"")
                for item in tail.strip().rstrip(").").strip("[]").split(",")
                if item.strip()
            ]
        }
    return envelope
class _null_context:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


def _font_block(binding: font_tools.FontBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {
        "variant": binding.variant,
        "available": binding.available,
        # A portable case is the contract everywhere; a system case can only be
        # checked on a machine whose fonts hash the same.
        "skip_unless_hash_matches": binding.variant == "system",
    }


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def preflight() -> dict[str, str]:
    """Refuse to produce a partial golden.

    A capture missing read_excel_sheet because openpyxl was absent looks
    complete and is not, so a missing library is fatal rather than a skip.
    raqm is fatal for a different reason: with it, Pillow shapes through
    HarfBuzz and applies kerning, and every advance in the manifest would be
    silently incompatible with one captured without it.
    """
    versions: dict[str, str] = {}
    missing: list[str] = []
    for module, extra in (
        ("openpyxl", "excel"), ("pptx", "pptx"), ("docx", "word"),
        ("pdfplumber", "pdf"), ("PIL", "pptx"), ("lxml", "capture-time only"),
        ("fontTools", "capture-time only"),
    ):
        try:
            versions[module] = getattr(__import__(module), "__version__", "unknown")
        except Exception:  # noqa: BLE001
            missing.append(f"{module} ({extra})")
    if missing:
        print("missing capture-time dependencies: " + ", ".join(missing), file=sys.stderr)
        print('install with: pip install -e ".[office]" lxml fonttools', file=sys.stderr)
        raise SystemExit(2)

    from PIL import features

    if features.check("raqm"):
        print(
            "Pillow reports raqm available. Text advances would be shaped "
            "through HarfBuzz with kerning applied, which is not what the "
            "target environment does and not what these goldens describe.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return versions


def capture(out: Path, variants: tuple[str, ...]) -> None:
    versions = preflight()

    fixture_bytes = fx.build_all()
    cases = case_table.all_cases(variants)

    bindings: dict[str, font_tools.FontBinding] = {}
    font_blocks: dict[str, Any] = {}
    charset = case_table.measured_charset(cases)
    for variant in variants:
        binding = font_tools.resolve_font_binding(variant)
        bindings[variant] = binding
        font_blocks[variant] = font_tools.font_block(binding, charset)
        if not binding.available:
            print(
                f"warning: no {variant} font resolved; its cases are recorded "
                "as skipped",
                file=sys.stderr,
            )

    records: list[dict[str, Any]] = []
    for case in cases:
        binding = bindings.get(case.font_variant) if case.font_variant else None
        if binding is not None and not binding.available:
            records.append({
                "id": case.id, "tool": case.tool, "args": case.args,
                "outcome": "skipped", "output": None, "error": None,
                "projection": None, "normalizations": [],
                "font": _font_block(binding), "note": case.note,
                "skip_reason": f"no {binding.variant} font resolved on this machine",
            })
            continue
        try:
            records.append(run_case(case, fixture_bytes, binding))
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            print(f"capture failed on case {case.id!r}", file=sys.stderr)
            raise SystemExit(3)

    out.mkdir(parents=True, exist_ok=True)
    fixtures_dir = out / "fixtures"
    if fixtures_dir.exists():
        shutil.rmtree(fixtures_dir)
    fixtures_dir.mkdir()
    for name, payload in sorted(fixture_bytes.items()):
        (fixtures_dir / name).write_bytes(payload)

    _write_json(out / "cases.json", {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "tools/golden/capture_office_goldens.py",
        "manifest": "manifest.json",
        "tokens": {
            WS_TOKEN: "the per-case temp workspace root",
            TMP_TOKEN: "its parent, which workspace-escape messages name",
        },
        "case_count": len(records),
        "cases": records,
    })
    _write_json(out / "manifest.json", _manifest(versions, fixture_bytes, font_blocks, charset))


def _manifest(versions: dict[str, str], fixture_bytes: dict[str, bytes],
              font_blocks: dict[str, Any], charset: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_against": "python-final",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": versions,
            **font_tools.system_note(),
        },
        "fixtures": {
            name: {"bytes": len(payload), "sha256": fx.sha256(payload)}
            for name, payload in sorted(fixture_bytes.items())
        },
        "fonts": font_blocks,
        "measured_charset": charset,
        "notes": {
            "float_formatting": (
                "Excel cell values reach the model through str(), so 1e-7 "
                "becomes the STRING \"1e-07\" and 1e16 becomes \"1e+16\". "
                "JavaScript's String() gives \"1e-7\" and "
                "\"10000000000000000\". The port must implement Python's "
                "repr(float): shortest round-trip, a two-digit signed "
                "exponent, and a forced .0 on an integral float. "
                "0.30000000000000004 agrees between the two languages and is "
                "in the fixture alongside the two that do not."
            ),
            "rounding": (
                "Every round() in the office layer is banker's rounding on "
                "the binary double, not Math.round. It governs shape "
                "dimensions (office.py:798), fill_ratio and used_height_pt "
                "(text_metrics.py:321-326) and every pdf_tables coordinate. "
                "The goldens record the Python value; they are not "
                "re-rounded, because re-rounding would hide exactly the bug "
                "they exist to catch."
            ),
            "shape_type_strings": (
                "str(shape.shape_type) comes from python-pptx's "
                "MSO_SHAPE_TYPE.__str__ and is recorded verbatim: "
                "\"TEXT_BOX (17)\", \"PLACEHOLDER (14)\", \"LINE (9)\" and so "
                "on. The port ships this as a lookup table."
            ),
            "vertical_tab": (
                "An a:br inside a paragraph surfaces in shape.text as \\u000b, "
                "which is what read_pptx returns."
            ),
            "hidden_column_range": (
                "read_excel_sheet/hidden-col-range reads the HiddenRange "
                "sheet, whose sole feature is <col min=3 max=5 hidden=1>. "
                "openpyxl collapses that into one ColumnDimension keyed 'C', "
                "so the Python hides column 3 only and reports five headers "
                "and '1 hidden column'; the port hides 3..5 and should report "
                "three headers and '3 hidden columns'. The sheet exists to "
                "keep that the ONE place a TypeScript output is expected to "
                "differ -- while the range sat on the Data sheet, twelve "
                "cases inherited it and this claim was wrong."
            ),
            "write_excel_cell_round_trip": (
                "The Python round-trips the workbook through openpyxl, which "
                "drops cached formula values and any chart or pivot part. The "
                "projection's formula_cells_losing_cached_v records that loss. "
                "chart_parts and pivot_parts sit alongside it but prove "
                "nothing here: databook.xlsx is hand-assembled and has "
                "neither, so both lists are empty before and after. Chart and "
                "pivot preservation -- one of the three things the port's "
                "in-place edit exists to fix -- has no coverage."
            ),
            "font_advances": (
                "Pillow measures hinted, grid-fit advances (raqm absent, "
                "Layout.BASIC, FT_LOAD_DEFAULT). On Arial at 9pt, 21 of the 95 "
                "printable ASCII glyphs differ from round(hmtx): C D F H J M N "
                "R T U W Z c k m s v w x y z. The vendored Noto Sans subset "
                "has zero such glyphs, so the portable cases alone cannot "
                "catch a port that computes advances instead of shipping the "
                "table. Both variants record advances_px, unhinted_px and "
                "round_unhinted_mismatches per glyph."
            ),
        },
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def check(out: Path, variants: tuple[str, ...]) -> int:
    scratch = Path(tempfile.mkdtemp(prefix="yucode-golden-check-"))
    try:
        capture(scratch, variants)
        blocking: list[str] = []
        environment: list[str] = []

        for name in ("cases.json", "manifest.json"):
            expected, produced = out / name, scratch / name
            if not expected.is_file():
                blocking.append(f"  + {name} (new)")
                continue
            if name == "cases.json" and expected.read_bytes() == produced.read_bytes():
                continue
            left = json.loads(expected.read_text(encoding="utf-8"))
            right = json.loads(produced.read_text(encoding="utf-8"))
            if name == "cases.json" and left == right:
                # Parses the same, different bytes: a _write_json change
                # rewrites every file while the parsed diff reports a match.
                blocking.append("  ! cases.json (serialisation changed; content identical)")
                continue
            if name == "manifest.json":
                # Library and OS versions legitimately move; the fonts and the
                # fixture hashes do not.
                for block in (left, right):
                    block.pop("environment", None)
                    # fonts.system names this machine's Arial and its sha256.
                    # The cases that use it already carry
                    # skip_unless_hash_matches, so treating it as blocking
                    # would fail every check run on another machine.
                    block.get("fonts", {}).pop("system", None)
                if left != right:
                    blocking.append("  ! manifest.json (fixtures or fonts drifted)")
                if json.loads(expected.read_text(encoding="utf-8")).get("environment") != \
                        json.loads(produced.read_text(encoding="utf-8")).get("environment"):
                    environment.append("  ~ manifest.json environment block (expected)")
            elif left != right:
                blocking.append(f"  ! {name} ({_first_case_diff(left, right)})")

        expected_fixtures = {p.name: p.read_bytes() for p in (out / "fixtures").glob("*")}
        produced_fixtures = {p.name: p.read_bytes() for p in (scratch / "fixtures").glob("*")}
        for name in sorted(set(expected_fixtures) | set(produced_fixtures)):
            if name not in expected_fixtures:
                blocking.append(f"  + fixtures/{name} (new)")
            elif name not in produced_fixtures:
                blocking.append(f"  - fixtures/{name} (no longer produced)")
            elif expected_fixtures[name] != produced_fixtures[name]:
                blocking.append(f"  ! fixtures/{name} (bytes differ)")

        for line in environment + blocking:
            print(line)
        if blocking:
            print(f"{len(blocking)} item(s) drifted")
            return 1
        print("office goldens match")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _first_case_diff(left: dict, right: dict) -> str:
    left_cases = {c["id"]: c for c in left.get("cases", [])}
    right_cases = {c["id"]: c for c in right.get("cases", [])}
    for case_id in sorted(set(left_cases) | set(right_cases)):
        if case_id not in left_cases:
            return f"new case {case_id}"
        if case_id not in right_cases:
            return f"case {case_id} no longer produced"
        if left_cases[case_id] != right_cases[case_id]:
            return f"first differing case: {case_id}"
    return "differs outside the case list"


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", nargs="?", default="capture",
        choices=["capture", "check", "vendor-fonts"],
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--font-variant", default="both", choices=["portable", "system", "both"],
    )
    parser.add_argument("--latin", type=Path, help="vendor-fonts: source Noto Sans TTF")
    parser.add_argument("--cjk", type=Path, help="vendor-fonts: source Noto Sans SC TTF")
    args = parser.parse_args(argv)

    variants = (
        font_tools.FONT_VARIANTS if args.font_variant == "both" else (args.font_variant,)
    )

    if args.command == "vendor-fonts":
        if not (args.latin and args.cjk):
            parser.error("vendor-fonts needs --latin and --cjk")
        charset = case_table.measured_charset(case_table.all_cases(variants))
        font_tools.vendor_fonts(args.latin, args.cjk, charset)
        print(f"wrote subsets to {font_tools.FONTS_DIR}")
        return 0

    if args.command == "check":
        return check(args.out, variants)

    capture(args.out, variants)
    print(f"wrote office goldens to {args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_SCRATCH_HOME, ignore_errors=True)
