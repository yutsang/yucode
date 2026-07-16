"""Office tools must fail with a clear, actionable message (no raw traceback)
when an optional dependency (openpyxl/python-docx/python-pptx/pdfplumber) is
missing — the fdd-commentary skill relies on this to fall back to the
databook .txt export instead of .xlsx. See AGENT_UPGRADE_NOTES.md section 3.
"""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.runtime import AgentRuntime
from coding_agent.tools.office import _require


def test_require_raises_clear_message_with_pip_extra_and_bare_install() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        _require("no_such_module_xyz", "some-package>=1.0", "excel")
    message = str(exc_info.value)
    assert "some-package>=1.0 is required for this tool" in message
    assert "pip install yucode-agent[excel]" in message
    assert "pip install some-package>=1.0" in message


@pytest.fixture()
def _no_openpyxl(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.fixture()
def runtime(tmp_path: Path) -> AgentRuntime:
    config = AppConfig(
        provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )
    return AgentRuntime(tmp_path, config)


def test_read_excel_sheet_fails_cleanly_without_openpyxl(runtime: AgentRuntime, _no_openpyxl) -> None:
    (runtime.workspace_root / "test.xlsx").write_bytes(b"not a real xlsx")
    output = runtime._execute_tool("read_excel_sheet", json.dumps({"path": "test.xlsx"}))

    # Must be the structured tool-error envelope, not a raw Python traceback.
    assert "Traceback" not in output
    assert "File \"" not in output
    payload = json.loads(output)
    assert payload["error_code"] == "tool_error"
    assert "openpyxl" in payload["error"]
    assert "pip install" in payload["error"]


def test_excel_to_json_fails_cleanly_without_openpyxl(runtime: AgentRuntime, _no_openpyxl) -> None:
    (runtime.workspace_root / "test.xlsx").write_bytes(b"not a real xlsx")
    output = runtime._execute_tool("excel_to_json", json.dumps({"path": "test.xlsx"}))
    assert "Traceback" not in output
    payload = json.loads(output)
    assert payload["error_code"] == "tool_error"
    assert "openpyxl" in payload["error"]
