"""Plugin system -- discovery, installation, and execution.

Plugins are directories containing a ``plugin.json`` manifest.  They can
provide tools (as external processes), commands, and lifecycle hooks.

Plugin locations:
  - Bundled: shipped with the agent
  - External: installed in ``.yucode/plugins/``
  - User can also point to arbitrary dirs via config
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PluginToolManifest:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    permission: str = "workspace-write"


@dataclass
class PluginCommandManifest:
    name: str
    description: str = ""


@dataclass
class PluginHooks:
    pre_tool_use: list[str] = field(default_factory=list)
    post_tool_use: list[str] = field(default_factory=list)


@dataclass
class PluginLifecycle:
    init: str = ""
    shutdown: str = ""


@dataclass
class PluginManifest:
    name: str
    version: str = "0.0.0"
    description: str = ""
    tools: list[PluginToolManifest] = field(default_factory=list)
    commands: list[PluginCommandManifest] = field(default_factory=list)
    hooks: PluginHooks = field(default_factory=PluginHooks)
    lifecycle: PluginLifecycle = field(default_factory=PluginLifecycle)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PluginManifest:
        tools_raw = d.get("tools", [])
        tools = [
            PluginToolManifest(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", t.get("input_schema", {"type": "object", "properties": {}})),
                permission=t.get("permission", "workspace-write"),
            )
            for t in tools_raw
        ]
        commands_raw = d.get("commands", [])
        commands = [
            PluginCommandManifest(name=c["name"], description=c.get("description", ""))
            for c in commands_raw
        ]
        hooks_raw = d.get("hooks", {})
        hooks = PluginHooks(
            pre_tool_use=hooks_raw.get("preToolUse", hooks_raw.get("pre_tool_use", [])),
            post_tool_use=hooks_raw.get("postToolUse", hooks_raw.get("post_tool_use", [])),
        )
        lifecycle_raw = d.get("lifecycle", {})
        lifecycle = PluginLifecycle(
            init=lifecycle_raw.get("init", ""),
            shutdown=lifecycle_raw.get("shutdown", ""),
        )
        return cls(
            name=d.get("name", ""),
            version=d.get("version", "0.0.0"),
            description=d.get("description", ""),
            tools=tools,
            commands=commands,
            hooks=hooks,
            lifecycle=lifecycle,
        )

    @classmethod
    def from_file(cls, path: Path) -> PluginManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


@dataclass
class PluginTool:
    """A tool provided by a plugin, executed as an external process."""
    name: str
    description: str
    input_schema: dict[str, Any]
    permission: str
    plugin_name: str
    plugin_dir: Path

    def execute(self, arguments: dict[str, Any]) -> str:
        env = dict(os.environ)
        env["YUCODE_PLUGIN_NAME"] = self.plugin_name
        env["YUCODE_TOOL_NAME"] = self.name
        env["YUCODE_TOOL_INPUT"] = json.dumps(arguments)
        env["YUCODE_PLUGIN_DIR"] = str(self.plugin_dir)

        tool_script = self.plugin_dir / "tools" / self.name
        if not tool_script.exists():
            tool_script = self.plugin_dir / f"tool_{self.name}"
        if not tool_script.exists():
            raise RuntimeError(
                f"Plugin tool executable not found for `{self.name}` "
                f"in {self.plugin_dir}"
            )

        result = subprocess.run(
            [str(tool_script)],
            input=json.dumps(arguments).encode(),
            capture_output=True,
            cwd=str(self.plugin_dir),
            env=env,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Plugin tool `{self.name}` failed (exit {result.returncode}): {stderr}"
            )
        return result.stdout.decode("utf-8", errors="replace")


class PluginRegistry:
    """Collects tools and hooks from all registered plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifest] = {}
        self._plugin_dirs: dict[str, Path] = {}
        self._tools: dict[str, PluginTool] = {}

    def register(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        if manifest.name in self._plugins:
            raise ValueError(f"Plugin `{manifest.name}` is already registered")
        self._plugins[manifest.name] = manifest
        self._plugin_dirs[manifest.name] = plugin_dir

        for tool_manifest in manifest.tools:
            if tool_manifest.name in self._tools:
                raise ValueError(
                    f"Duplicate plugin tool name `{tool_manifest.name}` "
                    f"(from plugin `{manifest.name}`)"
                )
            self._tools[tool_manifest.name] = PluginTool(
                name=tool_manifest.name,
                description=tool_manifest.description,
                input_schema=tool_manifest.input_schema,
                permission=tool_manifest.permission,
                plugin_name=manifest.name,
                plugin_dir=plugin_dir,
            )

    def all_tools(self) -> list[PluginTool]:
        return list(self._tools.values())

    def all_hook_commands(self) -> PluginHooks:
        pre: list[str] = []
        post: list[str] = []
        for manifest in self._plugins.values():
            pre.extend(manifest.hooks.pre_tool_use)
            post.extend(manifest.hooks.post_tool_use)
        return PluginHooks(pre_tool_use=pre, post_tool_use=post)

    def plugin_names(self) -> list[str]:
        return sorted(self._plugins)

    def get_manifest(self, name: str) -> PluginManifest | None:
        return self._plugins.get(name)

    def init_plugins(self) -> None:
        for name, manifest in self._plugins.items():
            if manifest.lifecycle.init:
                plugin_dir = self._plugin_dirs[name]
                subprocess.run(
                    manifest.lifecycle.init,
                    shell=True,
                    cwd=str(plugin_dir),
                    capture_output=True,
                )

    def shutdown_plugins(self) -> None:
        for name, manifest in self._plugins.items():
            if manifest.lifecycle.shutdown:
                plugin_dir = self._plugin_dirs[name]
                subprocess.run(
                    manifest.lifecycle.shutdown,
                    shell=True,
                    cwd=str(plugin_dir),
                    capture_output=True,
                )


class PluginManager:
    """High-level plugin management: discover, install, enable, disable."""

    MANIFEST_FILENAME = "plugin.json"

    def __init__(self, workspace_root: Path) -> None:
        from ..config.settings import state_dir
        self.workspace_root = workspace_root.resolve()
        self._plugins_dir = state_dir(self.workspace_root) / "plugins"
        self._registry_path = self._plugins_dir / "registry.json"

    @property
    def plugins_dir(self) -> Path:
        return self._plugins_dir

    def discover_plugins(self, extra_dirs: list[Path] | None = None) -> list[tuple[PluginManifest, Path]]:
        found: list[tuple[PluginManifest, Path]] = []
        search_dirs = [self._plugins_dir]
        if extra_dirs:
            search_dirs.extend(extra_dirs)
        for base in search_dirs:
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                manifest_path = child / self.MANIFEST_FILENAME
                if manifest_path.is_file():
                    try:
                        manifest = PluginManifest.from_file(manifest_path)
                        found.append((manifest, child))
                    except (json.JSONDecodeError, KeyError, OSError):
                        continue
        return found

    def install_local(self, source_dir: Path, name: str | None = None) -> PluginManifest:
        source = source_dir.resolve()
        manifest_path = source / self.MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"No {self.MANIFEST_FILENAME} found in {source}")
        manifest = PluginManifest.from_file(manifest_path)
        plugin_name = name or manifest.name
        dest = self._plugins_dir / plugin_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        self._update_registry(plugin_name, enabled=True)
        return manifest

    def install_git(self, repo_url: str, name: str | None = None) -> PluginManifest:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["git", "clone", "--depth=1", repo_url, tmp],
                check=True,
                capture_output=True,
            )
            manifest_path = Path(tmp) / self.MANIFEST_FILENAME
            if not manifest_path.is_file():
                raise FileNotFoundError(
                    f"Cloned repo does not contain {self.MANIFEST_FILENAME}"
                )
            manifest = PluginManifest.from_file(manifest_path)
            plugin_name = name or manifest.name
            dest = self._plugins_dir / plugin_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(tmp, dest)
        self._update_registry(plugin_name, enabled=True)
        return manifest

    def uninstall(self, name: str) -> None:
        dest = self._plugins_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        self._update_registry(name, remove=True)

    def enable(self, name: str) -> None:
        self._update_registry(name, enabled=True)

    def disable(self, name: str) -> None:
        self._update_registry(name, enabled=False)

    def is_enabled(self, name: str) -> bool:
        registry = self._load_registry()
        return registry.get(name, {}).get("enabled", False)

    def list_installed(self) -> list[dict[str, Any]]:
        registry = self._load_registry()
        plugins: list[dict[str, Any]] = []
        for name, info in sorted(registry.items()):
            entry: dict[str, Any] = {"name": name, "enabled": info.get("enabled", False)}
            manifest_path = self._plugins_dir / name / self.MANIFEST_FILENAME
            if manifest_path.is_file():
                try:
                    m = PluginManifest.from_file(manifest_path)
                    entry["version"] = m.version
                    entry["description"] = m.description
                except (json.JSONDecodeError, OSError):
                    pass
            plugins.append(entry)
        return plugins

    def build_registry(self, enabled_only: bool = True) -> PluginRegistry:
        registry = PluginRegistry()
        for manifest, plugin_dir in self.discover_plugins():
            if enabled_only and not self.is_enabled(manifest.name):
                continue
            registry.register(manifest, plugin_dir)
        return registry

    def _load_registry(self) -> dict[str, Any]:
        if not self._registry_path.is_file():
            return {}
        try:
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_registry(self, data: dict[str, Any]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    def _update_registry(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        remove: bool = False,
    ) -> None:
        registry = self._load_registry()
        if remove:
            registry.pop(name, None)
        else:
            entry = registry.setdefault(name, {})
            if enabled is not None:
                entry["enabled"] = enabled
        self._save_registry(registry)
