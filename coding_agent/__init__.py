"""YuCode -- Python-first coding agent runtime and VS Code bridge."""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


def _read_repo_version() -> str:
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject_path.is_file():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        version = project.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return "0.2.0"


__version__ = _read_repo_version()

from .config import AppConfig, load_app_config
from .core.errors import (
    AgentError,
    BudgetExhaustedError,
    CompactionError,
    ConfigError,
    McpError,
    PermissionDeniedError,
    ProviderError,
    ToolExecutionError,
)
from .core.runtime import AgentRuntime, TurnSummary
from .core.session import Session, Message, Usage, UsageTracker
from .security.permissions import PermissionPolicy, PermissionMode, PermissionPrompter
from .hooks import HookRunner, HookRunResult, HookConfig
from .memory.compact import compact_session, CompactionConfig, CompactionResult
from .plugins import PluginManager, PluginManifest, PluginRegistry
from .security.sandbox import SandboxConfig, SandboxStatus
from .tools import RiskLevel

__all__ = [
    "AgentError",
    "AgentRuntime",
    "AppConfig",
    "BudgetExhaustedError",
    "CompactionConfig",
    "CompactionError",
    "CompactionResult",
    "ConfigError",
    "HookConfig",
    "HookRunResult",
    "HookRunner",
    "McpError",
    "Message",
    "PermissionDeniedError",
    "PermissionMode",
    "PermissionPolicy",
    "PermissionPrompter",
    "PluginManager",
    "PluginManifest",
    "PluginRegistry",
    "ProviderError",
    "RiskLevel",
    "SandboxConfig",
    "SandboxStatus",
    "Session",
    "ToolExecutionError",
    "TurnSummary",
    "Usage",
    "UsageTracker",
    "compact_session",
    "load_app_config",
]
