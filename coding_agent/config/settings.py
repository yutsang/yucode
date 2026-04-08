from __future__ import annotations

import hashlib as _hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .simple_yaml import YamlError, dump_yaml, load_yaml

PermissionMode = Literal["read-only", "workspace-write", "danger-full-access", "prompt", "allow"]
McpTransport = Literal["stdio", "sse", "http", "ws"]
CompactStrategy = Literal["heuristic", "llm"]
ErrorStrategy = Literal["strict", "resilient"]
LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]
LogFormat = Literal["text", "json"]
StreamingMode = Literal["stream", "no_stream", "hybrid"]

BUNDLED_CONFIG_PATH = Path(__file__).resolve().with_name("config.yml")
DEFAULT_CONFIG_PATH = Path.home() / ".yucode" / "settings.yml"

_API_KEY_ENV_VARS = ("YUCODE_API_KEY",)

_HOME_YUCODE = Path.home() / ".yucode"


def workspace_key(workspace: Path) -> str:
    """Derive a stable short key from a workspace path for home-based state."""
    resolved = str(workspace.resolve())
    return _hashlib.sha256(resolved.encode()).hexdigest()[:12]


def state_dir(workspace: Path) -> Path:
    """Return the home-based state directory for a given workspace.

    Layout: ~/.yucode/projects/<workspace_key>/
    Falls back to workspace/.yucode if the legacy directory already exists
    and the home-based one does not (migration path).
    """
    home_state = _HOME_YUCODE / "projects" / workspace_key(workspace)
    legacy = workspace.resolve() / ".yucode"
    if home_state.exists() or not legacy.exists():
        return home_state
    return home_state


def _resolve_api_key(config_value: str) -> str:
    """Resolve API key: env var takes priority, then config file value."""
    for var in _API_KEY_ENV_VARS:
        env_val = os.environ.get(var, "").strip()
        if env_val:
            return env_val
    return config_value


@dataclass(frozen=True)
class ProviderConfig:
    name: str = ""
    type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    chat_path: str = "/chat/completions"
    append_chat_path: bool = True
    stream: bool = True
    streaming_mode: StreamingMode = "hybrid"
    temperature: float = 0.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


OrchestrationMode = Literal["auto", "single", "multi"]


@dataclass(frozen=True)
class RuntimeOptions:
    permission_mode: PermissionMode = "workspace-write"
    max_iterations: int = 25
    max_worker_steps: int = 20
    orchestration_mode: OrchestrationMode = "auto"
    parallel_workers: bool = False
    max_tool_calls: int = 80
    dedup_tool_threshold: int = 3
    auto_save_session: bool = True
    auto_resume_latest: bool = True
    compact_preserve_recent: int = 4
    compact_token_threshold: int = 10_000
    compact_strategy: CompactStrategy = "heuristic"
    error_strategy: ErrorStrategy = "resilient"
    shell_timeout_seconds: int = 30
    include_git_context: bool = True
    config_dump_in_prompt: bool = True


@dataclass(frozen=True)
class ToolOptions:
    allowed: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: McpTransport = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VscodeOptions:
    auto_start_backend: bool = True
    python_command: str = ""
    startup_timeout_seconds: int = 15


@dataclass(frozen=True)
class HookOptions:
    pre_tool_use: list[str] = field(default_factory=list)
    post_tool_use: list[str] = field(default_factory=list)
    post_tool_use_failure: list[str] = field(default_factory=list)
    pre_compact: list[str] = field(default_factory=list)
    post_compact: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PluginOptions:
    enabled_plugins: list[str] = field(default_factory=list)
    extra_dirs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionRuleConfig:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SandboxOptions:
    enabled: bool | None = None
    namespace_restrictions: bool | None = None
    network_isolation: bool | None = None
    filesystem_mode: str = "workspace-only"
    allowed_mounts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoggingOptions:
    level: LogLevel = "INFO"
    format: LogFormat = "text"
    file: str = ""


@dataclass(frozen=True)
class AuditOptions:
    enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)
    tools: ToolOptions = field(default_factory=ToolOptions)
    mcp: list[McpServerConfig] = field(default_factory=list)
    vscode: VscodeOptions = field(default_factory=VscodeOptions)
    instruction_files: list[str] = field(default_factory=list)
    hooks: HookOptions = field(default_factory=HookOptions)
    plugins: PluginOptions = field(default_factory=PluginOptions)
    sandbox: SandboxOptions = field(default_factory=SandboxOptions)
    permission_rules: PermissionRuleConfig = field(default_factory=PermissionRuleConfig)
    logging: LoggingOptions = field(default_factory=LoggingOptions)
    audit: AuditOptions = field(default_factory=AuditOptions)

    def as_prompt_safe_dict(self) -> dict[str, Any]:
        data = self.to_control_dict()
        if data["provider"].get("api_key"):
            data["provider"]["api_key"] = "***"
        return data

    def to_control_dict(self) -> dict[str, Any]:
        return {
            "provider": {
                "name": self.provider.name,
                "type": self.provider.type,
                "base_url": self.provider.base_url,
                "api_key": self.provider.api_key,
                "model": self.provider.model,
                "chat_path": self.provider.chat_path,
                "append_chat_path": self.provider.append_chat_path,
                "stream": self.provider.stream,
                "streaming_mode": self.provider.streaming_mode,
                "temperature": self.provider.temperature,
                "extra_headers": dict(self.provider.extra_headers),
                "extra_body": dict(self.provider.extra_body),
            },
            "runtime": {
                "permission_mode": self.runtime.permission_mode,
                "max_iterations": self.runtime.max_iterations,
                "max_worker_steps": self.runtime.max_worker_steps,
                "orchestration_mode": self.runtime.orchestration_mode,
                "parallel_workers": self.runtime.parallel_workers,
                "max_tool_calls": self.runtime.max_tool_calls,
                "dedup_tool_threshold": self.runtime.dedup_tool_threshold,
                "auto_save_session": self.runtime.auto_save_session,
                "auto_resume_latest": self.runtime.auto_resume_latest,
                "compact_preserve_recent": self.runtime.compact_preserve_recent,
                "compact_token_threshold": self.runtime.compact_token_threshold,
                "compact_strategy": self.runtime.compact_strategy,
                "error_strategy": self.runtime.error_strategy,
                "shell_timeout_seconds": self.runtime.shell_timeout_seconds,
                "include_git_context": self.runtime.include_git_context,
                "config_dump_in_prompt": self.runtime.config_dump_in_prompt,
            },
            "tools": {
                "allowed": list(self.tools.allowed),
                "disabled": list(self.tools.disabled),
            },
            "mcp": {
                "servers": [
                    {
                        "name": server.name,
                        "transport": server.transport,
                        "command": server.command,
                        "args": list(server.args),
                        "env": dict(server.env),
                        "url": server.url,
                        "headers": dict(server.headers),
                    }
                    for server in self.mcp
                ]
            },
            "vscode": {
                "auto_start_backend": self.vscode.auto_start_backend,
                "python_command": self.vscode.python_command,
                "startup_timeout_seconds": self.vscode.startup_timeout_seconds,
            },
            "hooks": {
                "pre_tool_use": list(self.hooks.pre_tool_use),
                "post_tool_use": list(self.hooks.post_tool_use),
                "post_tool_use_failure": list(self.hooks.post_tool_use_failure),
                "pre_compact": list(self.hooks.pre_compact),
                "post_compact": list(self.hooks.post_compact),
            },
            "plugins": {
                "enabled_plugins": list(self.plugins.enabled_plugins),
                "extra_dirs": list(self.plugins.extra_dirs),
            },
            "sandbox": {
                "enabled": self.sandbox.enabled,
                "namespace_restrictions": self.sandbox.namespace_restrictions,
                "network_isolation": self.sandbox.network_isolation,
                "filesystem_mode": self.sandbox.filesystem_mode,
                "allowed_mounts": list(self.sandbox.allowed_mounts),
            },
            "permission_rules": {
                "allow": list(self.permission_rules.allow),
                "deny": list(self.permission_rules.deny),
                "ask": list(self.permission_rules.ask),
            },
            "logging": {
                "level": self.logging.level,
                "format": self.logging.format,
                "file": self.logging.file,
            },
            "audit": {
                "enabled": self.audit.enabled,
            },
            "instruction_files": list(self.instruction_files),
        }


class ConfigError(ValueError):
    pass


def resolve_config_path(explicit_path: str | None = None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


def _default_config_text() -> str:
    if BUNDLED_CONFIG_PATH.is_file():
        return BUNDLED_CONFIG_PATH.read_text(encoding="utf-8")
    return dump_yaml(AppConfig().to_control_dict())


def ensure_default_config(path: Path | None = None) -> Path:
    target = path or resolve_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(_default_config_text(), encoding="utf-8")
    return target


def _resolve_workspace_mcp_path(workspace: Path | None = None) -> Path:
    base = (workspace or Path.cwd()).resolve()
    home_mcp = state_dir(base) / "mcp.yml"
    if home_mcp.is_file():
        return home_mcp
    legacy = base / ".yucode" / "mcp.yml"
    if legacy.is_file():
        return legacy
    return home_mcp


def _load_workspace_mcp_servers(workspace: Path | None = None) -> list[dict[str, Any]]:
    mcp_path = _resolve_workspace_mcp_path(workspace)
    if not mcp_path.is_file():
        return []
    try:
        raw = load_yaml(mcp_path.read_text(encoding="utf-8"))
    except YamlError:
        return []
    if not isinstance(raw, dict):
        return []
    servers = raw.get("servers", [])
    return servers if isinstance(servers, list) else []


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = list(result[key]) + value
        else:
            result[key] = value
    return result


def _load_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        import json as _json
        try:
            raw = _json.loads(text)
        except _json.JSONDecodeError:
            return {}
    else:
        try:
            raw = load_yaml(text)
        except YamlError:
            return {}
    return raw if isinstance(raw, dict) else {}


def discover_config_paths(workspace: Path | None = None) -> list[Path]:
    home = Path.home()
    cwd = (workspace or Path.cwd()).resolve()
    return [
        home / ".yucode" / "settings.yml",
        home / ".yucode" / "settings.json",
        cwd / ".yucode" / "settings.yml",
        cwd / ".yucode" / "settings.json",
        cwd / ".yucode" / "settings.local.yml",
        cwd / ".yucode" / "settings.local.json",
    ]


def load_app_config(explicit_path: str | None = None, workspace: Path | None = None) -> AppConfig:
    path = resolve_config_path(explicit_path)
    ensure_default_config(path)
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except YamlError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    raw_dict = _expect_dict(raw, "config")

    if not explicit_path:
        for overlay_path in discover_config_paths(workspace):
            overlay = _load_config_file(overlay_path)
            if overlay:
                raw_dict = _deep_merge(raw_dict, overlay)

    ws_servers = _load_workspace_mcp_servers(workspace)
    if ws_servers:
        mcp_container = raw_dict.setdefault("mcp", {})
        if not isinstance(mcp_container, dict):
            mcp_container = {}
            raw_dict["mcp"] = mcp_container
        existing = mcp_container.setdefault("servers", [])
        existing_names = {s.get("name") for s in existing if isinstance(s, dict)}
        for entry in ws_servers:
            if isinstance(entry, dict) and entry.get("name") not in existing_names:
                existing.append(entry)

    return app_config_from_dict(raw_dict)


def app_config_from_dict(raw: dict[str, Any]) -> AppConfig:
    provider_raw = _expect_dict(raw.get("provider", {}), "provider")
    runtime_raw = _expect_dict(raw.get("runtime", {}), "runtime")
    tools_raw = _expect_dict(raw.get("tools", {}), "tools")
    vscode_raw = _expect_dict(raw.get("vscode", {}), "vscode")
    mcp_container = _expect_dict(raw.get("mcp", {}), "mcp")
    mcp_raw = mcp_container.get("servers", [])
    instruction_files = raw.get("instruction_files", [])

    if not isinstance(mcp_raw, list):
        raise ConfigError("mcp.servers must be a list")
    if not isinstance(instruction_files, list) or not all(
        isinstance(item, str) for item in instruction_files
    ):
        raise ConfigError("instruction_files must be a list of strings")

    raw_api_key = str(provider_raw.get("api_key", ""))
    resolved_api_key = _resolve_api_key(raw_api_key)
    provider = ProviderConfig(
        name=os.environ.get("YUCODE_PROVIDER_NAME", "").strip() or str(provider_raw.get("name", "")),
        type=str(provider_raw.get("type", "openai_compatible")),
        base_url=(os.environ.get("YUCODE_BASE_URL", "").strip() or str(provider_raw.get("base_url", ""))).rstrip("/"),
        api_key=resolved_api_key,
        model=os.environ.get("YUCODE_MODEL", "").strip() or str(provider_raw.get("model", "")),
        chat_path=str(provider_raw.get("chat_path", "/chat/completions")),
        append_chat_path=bool(provider_raw.get("append_chat_path", True)),
        stream=bool(provider_raw.get("stream", True)),
        streaming_mode=_coerce_streaming_mode(provider_raw.get("streaming_mode", "")),
        temperature=float(provider_raw.get("temperature", 0.0)),
        extra_headers=_coerce_string_dict(provider_raw.get("extra_headers", {}), "provider.extra_headers"),
        extra_body=_expect_dict(provider_raw.get("extra_body", {}), "provider.extra_body"),
    )
    runtime = RuntimeOptions(
        permission_mode=_coerce_permission_mode(runtime_raw.get("permission_mode", "workspace-write")),
        max_iterations=_coerce_positive_int(runtime_raw.get("max_iterations", 12), "runtime.max_iterations"),
        max_worker_steps=_coerce_positive_int(runtime_raw.get("max_worker_steps", 20), "runtime.max_worker_steps"),
        orchestration_mode=_coerce_orchestration_mode(runtime_raw.get("orchestration_mode", "auto")),
        parallel_workers=bool(runtime_raw.get("parallel_workers", False)),
        max_tool_calls=_coerce_positive_int(runtime_raw.get("max_tool_calls", 80), "runtime.max_tool_calls"),
        dedup_tool_threshold=_coerce_positive_int(runtime_raw.get("dedup_tool_threshold", 3), "runtime.dedup_tool_threshold"),
        auto_save_session=bool(runtime_raw.get("auto_save_session", True)),
        auto_resume_latest=bool(runtime_raw.get("auto_resume_latest", True)),
        compact_preserve_recent=_coerce_positive_int(runtime_raw.get("compact_preserve_recent", 4), "runtime.compact_preserve_recent"),
        compact_token_threshold=_coerce_positive_int(runtime_raw.get("compact_token_threshold", 10000), "runtime.compact_token_threshold"),
        compact_strategy=_coerce_compact_strategy(runtime_raw.get("compact_strategy", "heuristic")),
        error_strategy=_coerce_error_strategy(runtime_raw.get("error_strategy", "resilient")),
        shell_timeout_seconds=_coerce_positive_int(
            runtime_raw.get("shell_timeout_seconds", 30),
            "runtime.shell_timeout_seconds",
        ),
        include_git_context=bool(runtime_raw.get("include_git_context", True)),
        config_dump_in_prompt=bool(runtime_raw.get("config_dump_in_prompt", True)),
    )
    tools = ToolOptions(
        allowed=_coerce_string_list(tools_raw.get("allowed", []), "tools.allowed"),
        disabled=_coerce_string_list(tools_raw.get("disabled", []), "tools.disabled"),
    )
    vscode = VscodeOptions(
        auto_start_backend=bool(vscode_raw.get("auto_start_backend", True)),
        python_command=str(vscode_raw.get("python_command", "")),
        startup_timeout_seconds=_coerce_positive_int(
            vscode_raw.get("startup_timeout_seconds", 15),
            "vscode.startup_timeout_seconds",
        ),
    )
    mcp = [mcp_server_from_dict(item) for item in mcp_raw]
    hooks_raw = _expect_dict(raw.get("hooks", {}), "hooks")
    hooks = HookOptions(
        pre_tool_use=_coerce_string_list(hooks_raw.get("pre_tool_use", []), "hooks.pre_tool_use"),
        post_tool_use=_coerce_string_list(hooks_raw.get("post_tool_use", []), "hooks.post_tool_use"),
        post_tool_use_failure=_coerce_string_list(hooks_raw.get("post_tool_use_failure", []), "hooks.post_tool_use_failure"),
        pre_compact=_coerce_string_list(hooks_raw.get("pre_compact", []), "hooks.pre_compact"),
        post_compact=_coerce_string_list(hooks_raw.get("post_compact", []), "hooks.post_compact"),
    )
    plugins_raw = _expect_dict(raw.get("plugins", {}), "plugins")
    plugins = PluginOptions(
        enabled_plugins=_coerce_string_list(plugins_raw.get("enabled_plugins", []), "plugins.enabled_plugins"),
        extra_dirs=_coerce_string_list(plugins_raw.get("extra_dirs", []), "plugins.extra_dirs"),
    )
    sandbox_raw = _expect_dict(raw.get("sandbox", {}), "sandbox")
    sandbox = SandboxOptions(
        enabled=sandbox_raw.get("enabled"),
        namespace_restrictions=sandbox_raw.get("namespace_restrictions"),
        network_isolation=sandbox_raw.get("network_isolation"),
        filesystem_mode=str(sandbox_raw.get("filesystem_mode", "workspace-only")),
        allowed_mounts=_coerce_string_list(sandbox_raw.get("allowed_mounts", []), "sandbox.allowed_mounts"),
    )
    logging_raw = _expect_dict(raw.get("logging", {}), "logging")
    logging_opts = LoggingOptions(
        level=_coerce_log_level(logging_raw.get("level", "INFO")),
        format=_coerce_log_format(logging_raw.get("format", "text")),
        file=str(logging_raw.get("file", "")),
    )
    audit_raw = _expect_dict(raw.get("audit", {}), "audit")
    audit_opts = AuditOptions(
        enabled=bool(audit_raw.get("enabled", True)),
    )
    perm_rules_raw = _expect_dict(raw.get("permission_rules", {}), "permission_rules")
    permission_rules = PermissionRuleConfig(
        allow=_coerce_string_list(perm_rules_raw.get("allow", []), "permission_rules.allow"),
        deny=_coerce_string_list(perm_rules_raw.get("deny", []), "permission_rules.deny"),
        ask=_coerce_string_list(perm_rules_raw.get("ask", []), "permission_rules.ask"),
    )
    return AppConfig(
        provider=provider,
        runtime=runtime,
        tools=tools,
        mcp=mcp,
        vscode=vscode,
        instruction_files=instruction_files,
        hooks=hooks,
        plugins=plugins,
        sandbox=sandbox,
        permission_rules=permission_rules,
        logging=logging_opts,
        audit=audit_opts,
    )


def mcp_server_from_dict(raw: Any) -> McpServerConfig:
    server = _expect_dict(raw, "mcp entry")
    transport = str(server.get("transport", "stdio"))
    if transport not in {"stdio", "sse", "http", "ws"}:
        raise ConfigError(f"Unsupported MCP transport: {transport}")
    name = str(server.get("name", "")).strip()
    if not name:
        raise ConfigError("Each MCP server must define a name")
    return McpServerConfig(
        name=name,
        transport=transport,  # type: ignore[arg-type]
        command=str(server.get("command", "")),
        args=_coerce_string_list(server.get("args", []), f"mcp[{name}].args"),
        env=_coerce_string_dict(server.get("env", {}), f"mcp[{name}].env"),
        url=str(server.get("url", "")),
        headers=_coerce_string_dict(server.get("headers", {}), f"mcp[{name}].headers"),
    )


# ---------------------------------------------------------------------------
# API key persistence
# ---------------------------------------------------------------------------

def set_api_key(
    api_key: str,
    config_path: str | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    name: str | None = None,
) -> Path:
    """Write the API key (and optional base_url/model/name) into config.yml."""
    path = resolve_config_path(config_path)
    ensure_default_config(path)
    raw = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    provider = raw.setdefault("provider", {})
    if not isinstance(provider, dict):
        provider = {}
        raw["provider"] = provider
    provider["api_key"] = api_key
    if base_url is not None:
        provider["base_url"] = base_url
    if model is not None:
        provider["model"] = model
    if name is not None:
        provider["name"] = name
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return path


def get_saved_api_key(config_path: str | None = None) -> str:
    """Read the saved API key from config.yml, or return empty string."""
    path = resolve_config_path(config_path)
    if not path.is_file():
        return ""
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except YamlError:
        return ""
    if not isinstance(raw, dict):
        return ""
    provider = raw.get("provider", {})
    if not isinstance(provider, dict):
        return ""
    return str(provider.get("api_key", "")).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def _coerce_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be a list of strings")
    return list(value)


def _coerce_string_dict(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ConfigError(f"{label} must be an object of string values")
    return dict(value)


def _coerce_positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{label} must be positive")
    return parsed


_PERMISSION_MODE_ALIASES: dict[str, PermissionMode] = {
    "plan": "read-only",
    "default": "read-only",
    "acceptEdits": "workspace-write",
    "auto": "workspace-write",
    "dontAsk": "danger-full-access",
}


def _coerce_permission_mode(value: Any) -> PermissionMode:
    text = str(value)
    if text in {"read-only", "workspace-write", "danger-full-access", "prompt", "allow"}:
        return text  # type: ignore[return-value]
    if text in _PERMISSION_MODE_ALIASES:
        return _PERMISSION_MODE_ALIASES[text]
    raise ConfigError(f"Unsupported permission mode: {text}")


def _coerce_orchestration_mode(value: Any) -> OrchestrationMode:
    text = str(value)
    if text not in {"auto", "single", "multi"}:
        raise ConfigError(f"Unsupported orchestration mode: {text}")
    return text  # type: ignore[return-value]


def _coerce_compact_strategy(value: Any) -> CompactStrategy:
    text = str(value)
    if text not in {"heuristic", "llm"}:
        raise ConfigError(f"Unsupported compact strategy: {text}")
    return text  # type: ignore[return-value]


def _coerce_error_strategy(value: Any) -> ErrorStrategy:
    text = str(value)
    if text not in {"strict", "resilient"}:
        raise ConfigError(f"Unsupported error strategy: {text}")
    return text  # type: ignore[return-value]


def _coerce_log_level(value: Any) -> LogLevel:
    text = str(value).upper()
    if text not in {"DEBUG", "INFO", "WARN", "ERROR"}:
        raise ConfigError(f"Unsupported log level: {text}")
    return text  # type: ignore[return-value]


def _coerce_streaming_mode(value: Any, *, stream_fallback: bool = True) -> StreamingMode:
    text = str(value).strip().lower()
    if text in {"stream", "no_stream", "hybrid"}:
        return text  # type: ignore[return-value]
    if text in ("", "auto"):
        return "hybrid"
    if text in ("true", "1", "yes"):
        return "stream"
    if text in ("false", "0", "no"):
        return "no_stream"
    return "hybrid"


def _coerce_log_format(value: Any) -> LogFormat:
    text = str(value).lower()
    if text not in {"text", "json"}:
        raise ConfigError(f"Unsupported log format: {text}")
    return text  # type: ignore[return-value]


def is_dangerous_mode() -> bool:
    """Check whether safety bypass mode is enabled via environment."""
    return os.environ.get("YUCODE_DANGEROUS_MODE", "0").strip() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Config mutation helpers (for CLI management commands)
# ---------------------------------------------------------------------------

def _mcp_server_entry(server: McpServerConfig) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": server.name, "transport": server.transport, "command": server.command}
    if server.args:
        entry["args"] = list(server.args)
    if server.env:
        entry["env"] = dict(server.env)
    if server.url:
        entry["url"] = server.url
    if server.headers:
        entry["headers"] = dict(server.headers)
    return entry


def add_mcp_server_to_config(
    server: McpServerConfig,
    config_path: str | None = None,
    *,
    workspace: Path | None = None,
) -> Path:
    if config_path:
        return _add_mcp_to_config_yml(server, resolve_config_path(config_path))
    mcp_path = _resolve_workspace_mcp_path(workspace)
    return _add_mcp_to_yaml(server, mcp_path)


def _add_mcp_to_config_yml(server: McpServerConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        raw = load_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = {}
    mcp_container = raw.setdefault("mcp", {})
    servers = mcp_container.setdefault("servers", [])
    if not isinstance(servers, list):
        servers = []
        mcp_container["servers"] = servers
    for existing in servers:
        if isinstance(existing, dict) and existing.get("name") == server.name:
            raise ConfigError(f"MCP server `{server.name}` already exists in config")
    servers.append(_mcp_server_entry(server))
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return path


def _add_mcp_to_yaml(server: McpServerConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        raw = load_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = {}
    servers = raw.setdefault("servers", [])
    if not isinstance(servers, list):
        servers = []
        raw["servers"] = servers
    for existing in servers:
        if isinstance(existing, dict) and existing.get("name") == server.name:
            raise ConfigError(f"MCP server `{server.name}` already exists in config")
    servers.append(_mcp_server_entry(server))
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return path


def remove_mcp_server_from_config(
    name: str,
    config_path: str | None = None,
    *,
    workspace: Path | None = None,
) -> Path:
    if config_path:
        return _remove_mcp_from_config_yml(name, resolve_config_path(config_path))
    mcp_path = _resolve_workspace_mcp_path(workspace)
    return _remove_mcp_from_yaml(name, mcp_path)


def _remove_mcp_from_config_yml(name: str, path: Path) -> Path:
    if not path.is_file():
        raise ConfigError(f"MCP server `{name}` not found in config")
    raw = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"MCP server `{name}` not found in config")
    mcp_container = raw.get("mcp", {})
    servers = mcp_container.get("servers", [])
    if not isinstance(servers, list):
        raise ConfigError(f"MCP server `{name}` not found in config")
    original_len = len(servers)
    servers[:] = [s for s in servers if not (isinstance(s, dict) and s.get("name") == name)]
    if len(servers) == original_len:
        raise ConfigError(f"MCP server `{name}` not found in config")
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return path


def _remove_mcp_from_yaml(name: str, path: Path) -> Path:
    if not path.is_file():
        raise ConfigError(f"MCP server `{name}` not found in config")
    raw = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"MCP server `{name}` not found in config")
    servers = raw.get("servers", [])
    if not isinstance(servers, list):
        raise ConfigError(f"MCP server `{name}` not found in config")
    original_len = len(servers)
    servers[:] = [s for s in servers if not (isinstance(s, dict) and s.get("name") == name)]
    if len(servers) == original_len:
        raise ConfigError(f"MCP server `{name}` not found in config")
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return path
