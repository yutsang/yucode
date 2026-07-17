"""bash tool must decode subprocess output as UTF-8, not the OS locale
default. On a Western-locale Windows box, Python's subprocess text=True
(without an explicit encoding) decodes as cp1252, which crashes with a
UnicodeDecodeError on any non-ASCII output (e.g. Chinese account names/
remarks in an FDD databook) instead of returning it. Found via a real
crash loop while running the fdd-commentary skill on a Chinese-language
databook: 'Exception in thread ... cp1252.py ... UnicodeDecodeError'.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.runtime import AgentRuntime


@pytest.fixture()
def runtime(tmp_path: Path) -> AgentRuntime:
    config = AppConfig(
        provider=ProviderConfig(base_url="http://x", api_key="k", model="m"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )
    return AgentRuntime(tmp_path, config)


class TestBashSubprocessEncoding:
    """White-box: assert the exact subprocess.run kwargs, so this fails
    against the old code regardless of which locale the test happens to run
    under (this Mac's default locale is already UTF-8, so a purely
    behavioural test wouldn't reproduce the Windows cp1252 crash)."""

    def test_no_sandbox_path_passes_utf8_encoding(self, runtime: AgentRuntime) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.stderr = ""
            runtime._execute_tool("bash", json.dumps({"command": "echo hi"}))
            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs.get("encoding") == "utf-8"
            assert kwargs.get("errors") == "replace"
            assert kwargs.get("text") is True


def test_bash_handles_utf8_output_end_to_end(runtime: AgentRuntime) -> None:
    """Functional check: a command that prints non-ASCII text must come back
    intact (or at least not crash), not raise UnicodeDecodeError."""
    python = sys.executable
    output = runtime._execute_tool(
        "bash",
        json.dumps({"command": f'{python} -c "print(\'上海松江\')"'}),
    )
    payload = json.loads(output)
    assert payload["returncode"] == 0
    assert "上海松江" in payload["stdout"]
