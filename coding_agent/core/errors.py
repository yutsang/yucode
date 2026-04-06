"""Structured error hierarchy for the agent runtime.

All agent-specific exceptions inherit from AgentError, which carries
``recoverable`` and ``category`` metadata so callers can decide how to
handle failures (retry, degrade gracefully, or abort).
"""

from __future__ import annotations

import json
from typing import Any


class AgentError(Exception):
    """Base for all agent errors."""

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool = True,
        category: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.recoverable = recoverable
        self.category = category

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "error_code": self.category,
            "recoverable": self.recoverable,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class ProviderError(AgentError):
    """LLM API / HTTP failures."""

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message, recoverable=recoverable, category="provider_error")


class ToolExecutionError(AgentError):
    """A tool handler raised an unexpected exception."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        recoverable: bool = True,
        suggestion: str = "",
    ) -> None:
        super().__init__(message, recoverable=recoverable, category="tool_error")
        self.tool_name = tool_name
        self.suggestion = suggestion

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


class PermissionDeniedError(AgentError):
    """Tool execution blocked by permission policy."""

    def __init__(self, message: str, *, tool_name: str = "") -> None:
        super().__init__(message, recoverable=False, category="permission_denied")
        self.tool_name = tool_name


class CompactionError(AgentError):
    """Failure during session compaction."""

    def __init__(self, message: str) -> None:
        super().__init__(message, recoverable=True, category="compaction_error")


class ConfigError(AgentError):
    """Configuration loading or validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message, recoverable=False, category="config_error")


class McpError(AgentError):
    """MCP server communication failure."""

    def __init__(self, message: str, *, server_name: str = "", recoverable: bool = True) -> None:
        super().__init__(message, recoverable=recoverable, category="mcp_error")
        self.server_name = server_name


class BudgetExhaustedError(AgentError):
    """Token budget or tool call limit exceeded."""

    def __init__(self, message: str) -> None:
        super().__init__(message, recoverable=False, category="budget_exhausted")


def tool_error_response(
    message: str,
    *,
    error_code: str = "tool_error",
    recoverable: bool = True,
    suggestion: str = "",
) -> str:
    """Build a structured JSON error string for tool results."""
    d: dict[str, Any] = {
        "error": message,
        "error_code": error_code,
        "recoverable": recoverable,
    }
    if suggestion:
        d["suggestion"] = suggestion
    return json.dumps(d, indent=2)
