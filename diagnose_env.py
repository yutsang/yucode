"""Standalone environment diagnostic for locked-down machines.

Run on the target machine from the repo root (no pip install, no pytest,
no admin rights needed):

    python diagnose_env.py
    python diagnose_env.py --network        # also probe gateway reachability
    python diagnose_env.py --workspace D:\\work\\proj

Paste the FULL output back for analysis. Every check is wrapped so one
failure never stops the rest; the script itself never needs network access
unless --network is passed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

_RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def _record(status: str, name: str, detail: str = "") -> None:
    _RESULTS.append((status, name, detail))
    line = f"[{status}] {name}"
    if detail:
        line += f": {detail}"
    print(line, flush=True)


def _fail_detail(exc: BaseException) -> str:
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tail = "".join(tb[-2:]).strip().replace("\n", " | ")
    return tail[:400]


def check(name: str):
    """Decorator: run a check function, catching everything."""
    def wrap(fn):
        def run(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                _record("FAIL", name, _fail_detail(exc))
        run._check_name = name
        return run
    return wrap


# ---------------------------------------------------------------------------
# Section 1: environment survey (INFO only, never fails)
# ---------------------------------------------------------------------------

def survey_environment(workspace: Path) -> None:
    print("=" * 72)
    print("SECTION 1: ENVIRONMENT")
    print("=" * 72)
    _record("INFO", "python", f"{sys.version.split()[0]} at {sys.executable}")
    _record("INFO", "platform", platform.platform())
    _record("INFO", "stdout_encoding_original", _ORIGINAL_STDOUT_ENCODING or "?")
    _record("INFO", "locale_preferred_encoding", locale.getpreferredencoding(False))
    _record("INFO", "filesystem_encoding", sys.getfilesystemencoding())
    _record("INFO", "home", str(Path.home()))
    _record("INFO", "workspace", str(workspace))
    _record("INFO", "repo_root", str(REPO_ROOT))

    # Code identity: what checkout is this?
    git = shutil.which("git")
    if git:
        try:
            head = subprocess.run(
                [git, "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=False,
            )
            _record("INFO", "git_commit", head.stdout.strip() or f"(rc={head.returncode})")
        except Exception as exc:  # noqa: BLE001
            _record("WARN", "git_commit", f"git present but failed: {exc}")
    else:
        _record("INFO", "git_commit", "(git not on PATH)")

    for binary in ("git", "rg", "fc-match"):
        path = shutil.which(binary)
        _record("INFO", f"binary_{binary}", path or "NOT FOUND")

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "YUCODE_API_KEY"):
        # Never print values: proxy URLs can embed credentials.
        _record("INFO", f"env_{var}", "set" if os.environ.get(var) else "not set")

    for module, label in (
        ("prompt_toolkit", "prompt_toolkit"),
        ("openpyxl", "openpyxl"),
        ("pptx", "python-pptx"),
        ("PIL", "Pillow"),
        ("pdfplumber", "pdfplumber"),
        ("docx", "python-docx"),
    ):
        try:
            mod = __import__(module)
            version = getattr(mod, "__version__", getattr(mod, "VERSION", "?"))
            _record("INFO", f"dep_{label}", f"{version}")
        except ImportError:
            _record("INFO", f"dep_{label}", "NOT INSTALLED")


# ---------------------------------------------------------------------------
# Section 2: filesystem & encoding checks
# ---------------------------------------------------------------------------

@check("home_state_dir_writable")
def check_home_writable(workspace: Path) -> None:
    from coding_agent.config.settings import state_dir
    target_dir = state_dir(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    probe = target_dir / "_diagnostic_probe.txt"
    probe.write_text("probe", encoding="utf-8")
    probe.unlink()
    _record("PASS", "home_state_dir_writable", str(target_dir))


@check("atomic_write_on_state_dir")
def check_atomic_write(workspace: Path) -> None:
    """os.replace semantics on the REAL state-dir filesystem — network-mapped
    home drives sometimes have broken rename-over-existing behavior."""
    from coding_agent.config.settings import state_dir
    from coding_agent.core.atomic_io import atomic_write_text
    target = state_dir(workspace) / "_diagnostic_atomic.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, '{"round": 1}')
    atomic_write_text(target, '{"round": 2}')  # replace over existing
    content = json.loads(target.read_text(encoding="utf-8"))
    target.unlink()
    assert content == {"round": 2}, f"unexpected content: {content}"
    _record("PASS", "atomic_write_on_state_dir", "write + replace-over-existing OK")


@check("session_save_load_roundtrip")
def check_session_roundtrip(workspace: Path) -> None:
    from coding_agent.core.session import Message, Session
    session = Session(model="diagnostic")
    session.add_message(Message(role="user", content="診斷測試 diagnostic ✓"))
    path = session.save_to_workspace(workspace, "_diagnostic_session")
    loaded = Session.load(path)
    path.unlink()
    assert "診斷測試" in loaded.messages[0].content
    _record("PASS", "session_save_load_roundtrip", "CJK content survived save/load")


@check("console_cjk_output")
def check_console_cjk() -> None:
    """Would CJK output crash on the ORIGINAL console encoding (before this
    script's own UTF-8 reconfigure)? yucode's CLI now reconfigures the same
    way, so strict-encoding failure here is a WARN (pre-fix behavior), not
    a FAIL."""
    sample = "示意性調整後 – 中文輸出測試"
    enc = _ORIGINAL_STDOUT_ENCODING or "ascii"
    try:
        sample.encode(enc, errors="strict")
        _record("PASS", "console_cjk_output", f"original console encoding {enc} handles CJK")
    except UnicodeEncodeError:
        _record(
            "WARN", "console_cjk_output",
            f"original console encoding {enc} can NOT encode CJK — yucode's "
            "startup UTF-8 reconfigure is load-bearing on this machine",
        )


@check("long_path_support")
def check_long_paths() -> None:
    if os.name != "nt":
        _record("SKIP", "long_path_support", "not Windows")
        return
    base = Path(tempfile.mkdtemp(prefix="yucode_diag_"))
    try:
        deep = base
        segment = "d" * 40
        while len(str(deep)) < 270:
            deep = deep / segment
        deep.mkdir(parents=True, exist_ok=True)
        probe = deep / "probe.txt"
        probe.write_text("x", encoding="utf-8")
        _record("PASS", "long_path_support", f"created {len(str(probe))}-char path")
    except OSError as exc:
        _record(
            "WARN", "long_path_support",
            f"paths >260 chars fail ({exc.__class__.__name__}) — deep dirs "
            "(node_modules-style) may error; keep workspaces shallow",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Section 3: yucode functional checks (this round's hardening, on real machine)
# ---------------------------------------------------------------------------

@check("config_load")
def check_config_load(workspace: Path) -> None:
    from coding_agent.config import load_app_config
    config = load_app_config(workspace=workspace)
    _record(
        "PASS", "config_load",
        f"provider={config.provider.name or '(unnamed)'} model={config.provider.model} "
        f"permission={config.runtime.permission_mode} "
        f"verify_tls={config.provider.verify_tls} ca_bundle={'set' if config.provider.ca_bundle else 'not set'} "
        f"api_key={'set' if config.provider.api_key else 'MISSING'}",
    )


@check("git_context_resilience")
def check_git_resilience(workspace: Path) -> None:
    from coding_agent.memory.prompting import _run_git
    started = time.monotonic()
    result = _run_git(workspace, ["status", "--short", "--branch"])
    elapsed = time.monotonic() - started
    if result is None:
        _record("PASS", "git_context_resilience",
                f"no git context (git missing/failed) handled cleanly in {elapsed:.1f}s — no crash")
    else:
        _record("PASS", "git_context_resilience",
                f"git status OK in {elapsed:.1f}s ({len(result.splitlines())} lines)")


@check("tls_context_build")
def check_tls_context(workspace: Path) -> None:
    from coding_agent.config import load_app_config
    from coding_agent.core.providers import OpenAICompatibleProvider
    config = load_app_config(workspace=workspace)
    provider = OpenAICompatibleProvider(config.provider)
    context = provider._ssl_context()
    if not config.provider.verify_tls:
        mode = "UNVERIFIED (verify_tls: false — consider ca_bundle instead)"
    elif config.provider.ca_bundle:
        mode = f"custom CA bundle: {config.provider.ca_bundle}"
    else:
        mode = "system default verification"
    assert context is not None or config.provider.verify_tls
    _record("PASS", "tls_context_build", mode)


@check("circuit_breaker_unit")
def check_circuit_breaker() -> None:
    from coding_agent.core import providers as pm
    pm._reset_circuit_breakers()
    host = "_diagnostic_host:1"
    for _ in range(pm._BREAKER_FAILURE_THRESHOLD):
        pm._breaker_record_connection_failure(host)
    assert pm._breaker_remaining_cooldown(host) is not None, "breaker did not open"
    pm._breaker_record_success(host)
    assert pm._breaker_remaining_cooldown(host) is None, "breaker did not reset"
    pm._reset_circuit_breakers()
    _record("PASS", "circuit_breaker_unit", "opens after threshold, resets on success")


@check("pure_python_file_walk")
def check_pure_walk(workspace: Path) -> None:
    """The no-ripgrep fallback path — always taken on machines without rg."""
    from coding_agent.tools.filesystem import _py_grep_lines, _py_list_files
    started = time.monotonic()
    files = _py_list_files(workspace)
    list_elapsed = time.monotonic() - started
    started = time.monotonic()
    hits = _py_grep_lines("def ", workspace, workspace, max_lines=20)
    grep_elapsed = time.monotonic() - started
    bad = [f for f in files if "node_modules" in f or f"{os.sep}.git{os.sep}" in f]
    assert not bad, f"artifact dirs not pruned: {bad[:3]}"
    status = "PASS" if (list_elapsed < 30 and grep_elapsed < 30) else "WARN"
    _record(status, "pure_python_file_walk",
            f"list {len(files)} files in {list_elapsed:.1f}s; grep 20 hits in {grep_elapsed:.1f}s "
            f"({len(hits)} found)")


@check("shell_tool_cjk_subprocess")
def check_shell_tool(workspace: Path) -> None:
    """End-to-end: the bash tool spawning a Python subprocess that prints
    CJK — covers the Windows shell path AND the utf-8 decode pinning."""
    from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
    from coding_agent.tools import ToolRegistry
    with tempfile.TemporaryDirectory(prefix="yucode_diag_") as tmp:
        config = AppConfig(
            provider=ProviderConfig(base_url="http://unused", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", shell_timeout_seconds=60),
        )
        registry = ToolRegistry(Path(tmp), config)

        # Probe 1: what encoding does a child python get on a PIPE through
        # the bash tool? (The locale code page — cp1252 — unless the tool
        # injects PYTHONIOENCODING; the console being UTF-8 is irrelevant
        # to pipes.) This is reported even when the main check passes.
        enc_cmd = f'"{sys.executable}" -c "import sys; print(sys.stdout.encoding)"'
        enc_payload = json.loads(registry.execute("bash", {"command": enc_cmd}))
        _record("INFO", "child_python_pipe_encoding",
                f"{enc_payload.get('stdout', '').strip() or '(no output)'} "
                f"(rc={enc_payload.get('returncode')})")

        # Probe 2: quoted-path CJK end-to-end.
        script = Path(tmp) / "cjk_probe.py"
        script.write_text("print('中文子行程輸出OK')\n", encoding="utf-8")
        command = f'"{sys.executable}" "{script}"'
        out = registry.execute("bash", {"command": command})
        payload = json.loads(out)
        stdout = payload.get("stdout", "")
        if "中文子行程輸出OK" in stdout:
            _record("PASS", "shell_tool_cjk_subprocess",
                    "spawned quoted-path subprocess via platform shell, CJK stdout intact")
        else:
            # Multi-line failure dump: the first Windows round-trip lost the
            # stderr tail to console line clipping when it was inlined.
            _record("FAIL", "shell_tool_cjk_subprocess",
                    "details on the indented lines below")
            print(f"    command: {command}")
            print(f"    rc     : {payload.get('returncode')!r}")
            print(f"    stdout : {stdout[:200]!r}")
            stderr_text = str(payload.get("stderr", ""))
            for line in (stderr_text.splitlines() or ["(empty)"])[:15]:
                print(f"    stderr | {line}")


@check("mcp_timeout_behavior")
def check_mcp_timeout() -> None:
    """Spawn a real never-responding subprocess as a fake MCP server; the
    client must give up within the (shortened) timeout, not hang."""
    from coding_agent.config import McpServerConfig
    from coding_agent.core.errors import McpError
    from coding_agent.plugins import mcp as mcp_mod
    original = mcp_mod._MCP_CALL_TIMEOUT_SECONDS
    mcp_mod._MCP_CALL_TIMEOUT_SECONDS = 3.0
    try:
        client = mcp_mod.StdioMcpClient(McpServerConfig(
            name="_diag_silent", transport="stdio",
            command=sys.executable, args=["-c", "import time; time.sleep(60)"],
        ))
        started = time.monotonic()
        try:
            client.list_tools()
            _record("FAIL", "mcp_timeout_behavior", "expected timeout error, got success!?")
        except McpError:
            elapsed = time.monotonic() - started
            if elapsed < 15:
                _record("PASS", "mcp_timeout_behavior", f"timed out cleanly in {elapsed:.1f}s")
            else:
                _record("WARN", "mcp_timeout_behavior", f"timed out but took {elapsed:.1f}s")
        finally:
            client.shutdown()
    finally:
        mcp_mod._MCP_CALL_TIMEOUT_SECONDS = original


@check("prompt_toolkit_windows_input")
def check_prompt_toolkit() -> None:
    try:
        import prompt_toolkit
    except ImportError:
        _record("WARN", "prompt_toolkit_windows_input",
                "prompt_toolkit NOT installed — REPL falls back to plain input() "
                "(no history/completion; paste works but differently)")
        return
    detail = f"prompt_toolkit {prompt_toolkit.__version__}"
    if os.name == "nt":
        from prompt_toolkit.input.win32 import Win32Input  # noqa: F401
        detail += "; Win32 console input reader importable"
    _record("PASS", "prompt_toolkit_windows_input", detail)
    _record("INFO", "paste_fix_manual_test",
            "MANUAL: run the REPL, paste a multi-line text — it must arrive as ONE "
            "prompt (newlines inserted), not submit on the first line")


# ---------------------------------------------------------------------------
# Section 4: network probes (opt-in)
# ---------------------------------------------------------------------------

@check("gateway_reachability")
def check_network(workspace: Path) -> None:
    import urllib.error
    import urllib.parse
    import urllib.request

    from coding_agent.config import load_app_config
    from coding_agent.core.providers import OpenAICompatibleProvider

    config = load_app_config(workspace=workspace)
    if not config.provider.base_url:
        _record("SKIP", "gateway_reachability", "no base_url configured")
        return
    provider = OpenAICompatibleProvider(config.provider)
    url = provider._build_url()
    host = urllib.parse.urlsplit(url).netloc
    context = provider._ssl_context()
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=context):
            pass
        _record("PASS", "gateway_reachability", f"{host} reachable (unexpected 2xx without auth)")
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        # ANY HTTP status proves TCP+TLS+proxy all work end to end.
        _record("PASS", "gateway_reachability",
                f"{host} reachable in {elapsed:.1f}s (HTTP {exc.code} without auth — expected)")
    except urllib.error.URLError as exc:
        elapsed = time.monotonic() - started
        _record("FAIL", "gateway_reachability",
                f"{host} NOT reachable in {elapsed:.1f}s: {exc.reason} "
                f"(proxy env {'set' if os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') else 'NOT set'})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_ORIGINAL_STDOUT_ENCODING: str | None = None


def main() -> int:
    global _ORIGINAL_STDOUT_ENCODING
    _ORIGINAL_STDOUT_ENCODING = getattr(sys.stdout, "encoding", None)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(io.UnsupportedOperation, ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="yucode environment diagnostic")
    parser.add_argument("--workspace", default=str(REPO_ROOT))
    parser.add_argument("--network", action="store_true",
                        help="Also probe provider gateway reachability (no credentials sent).")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()

    print("yucode environment diagnostic")
    print(f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    survey_environment(workspace)

    print()
    print("=" * 72)
    print("SECTION 2: FILESYSTEM & ENCODING")
    print("=" * 72)
    check_home_writable(workspace)
    check_atomic_write(workspace)
    check_session_roundtrip(workspace)
    check_console_cjk()
    check_long_paths()

    print()
    print("=" * 72)
    print("SECTION 3: YUCODE FUNCTIONAL CHECKS")
    print("=" * 72)
    check_config_load(workspace)
    check_git_resilience(workspace)
    check_tls_context(workspace)
    check_circuit_breaker()
    check_pure_walk(workspace)
    check_shell_tool(workspace)
    check_mcp_timeout()
    check_prompt_toolkit()

    print()
    print("=" * 72)
    print("SECTION 4: NETWORK (opt-in)")
    print("=" * 72)
    if args.network:
        check_network(workspace)
    else:
        _record("SKIP", "gateway_reachability",
                "pass --network to probe (sends an unauthenticated POST to your gateway; "
                "use test_connection.py --azure for the full authenticated probe)")

    print()
    print("=" * 72)
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0, "INFO": 0}
    for status, _, _ in _RESULTS:
        counts[status] = counts.get(status, 0) + 1
    print(f"SUMMARY: {counts['PASS']} passed, {counts['FAIL']} failed, "
          f"{counts['WARN']} warnings, {counts['SKIP']} skipped")
    if counts["FAIL"]:
        print("Failed checks:")
        for status, name, detail in _RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
    print()
    print(">>> 請把以上完整輸出（從第一行開始）貼回給 Claude 分析 <<<")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
