from __future__ import annotations

import json
import os

from coding_agent import __version__
from coding_agent.config.settings import _resolve_api_key, is_dangerous_mode
from coding_agent.core.errors import AgentError, tool_error_response
from coding_agent.core.session import Message
from coding_agent.memory.compact import CompactionConfig, compact_session, should_compact
from coding_agent.security.permissions import PermissionPolicy


def test_version_matches_repo() -> None:
    assert __version__ == "0.3.0"


def test_env_api_key_takes_priority() -> None:
    previous = os.environ.get("YUCODE_API_KEY")
    os.environ["YUCODE_API_KEY"] = "env-secret"
    try:
        assert _resolve_api_key("config-secret") == "env-secret"
    finally:
        if previous is None:
            os.environ.pop("YUCODE_API_KEY", None)
        else:
            os.environ["YUCODE_API_KEY"] = previous


def test_dangerous_mode_defaults_false() -> None:
    previous = os.environ.get("YUCODE_DANGEROUS_MODE")
    os.environ.pop("YUCODE_DANGEROUS_MODE", None)
    try:
        assert is_dangerous_mode() is False
    finally:
        if previous is not None:
            os.environ["YUCODE_DANGEROUS_MODE"] = previous


def test_tool_error_response_is_structured() -> None:
    payload = json.loads(
        tool_error_response(
            "failed",
            error_code="provider_error",
            recoverable=False,
            suggestion="retry later",
        )
    )
    assert payload == {
        "error": "failed",
        "error_code": "provider_error",
        "recoverable": False,
        "suggestion": "retry later",
    }


def test_agent_error_to_dict() -> None:
    err = AgentError("boom", recoverable=True, category="demo")
    assert err.to_dict()["error_code"] == "demo"


def test_permission_policy_denies_write_in_read_only() -> None:
    decision = PermissionPolicy("read-only").authorize("workspace-write", "write_file")
    assert decision.allowed is False


def test_compaction_preserves_recent_messages() -> None:
    messages = [Message(role="user", content="x" * 3000) for _ in range(8)]
    config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=100)
    assert should_compact(messages, config) is True
    result = compact_session(messages, config)
    assert result.removed_message_count > 0
    assert result.compacted_messages[0].role == "system"
    assert len(result.compacted_messages) == 3
