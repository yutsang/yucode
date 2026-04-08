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
    return "0.3.2"


__version__ = _read_repo_version()

from .config import AppConfig, load_app_config  # noqa: E402
from .core.errors import (  # noqa: E402
    AgentError,
    BudgetExhaustedError,
    CompactionError,
    ConfigError,
    McpError,
    PermissionDeniedError,
    ProviderError,
    ToolExecutionError,
)
from .core.runtime import AgentRuntime, TurnSummary  # noqa: E402
from .core.session import Message, Session, Usage, UsageTracker  # noqa: E402
from .hooks import HookConfig, HookRunner, HookRunResult  # noqa: E402
from .memory.compact import CompactionConfig, CompactionResult, compact_session  # noqa: E402
from .plugins import PluginManager, PluginManifest, PluginRegistry  # noqa: E402
from .security.permissions import (  # noqa: E402
    EnforcementResult,
    PermissionContext,
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
    PermissionPrompter,
    PermissionRules,
)
from .security.sandbox import SandboxConfig, SandboxStatus  # noqa: E402
from .tools import RiskLevel  # noqa: E402

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
    "PermissionEnforcer",
    "PermissionRules",
    "PermissionContext",
    "EnforcementResult",
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
