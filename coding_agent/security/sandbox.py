"""Sandbox and filesystem isolation.

Detects container environments and provides optional Linux namespace
isolation via ``unshare``.  Filesystem isolation modes restrict tool
access to the workspace directory.
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FilesystemIsolationMode(Enum):
    OFF = "off"
    WORKSPACE_ONLY = "workspace-only"
    ALLOW_LIST = "allow-list"


@dataclass
class SandboxConfig:
    enabled: bool | None = None
    namespace_restrictions: bool | None = None
    network_isolation: bool | None = None
    filesystem_mode: FilesystemIsolationMode | None = None
    allowed_mounts: list[str] = field(default_factory=list)

    def resolve_request(
        self,
        enabled_override: bool | None = None,
        namespace_override: bool | None = None,
        network_override: bool | None = None,
        filesystem_mode_override: FilesystemIsolationMode | None = None,
        allowed_mounts_override: list[str] | None = None,
    ) -> SandboxRequest:
        return SandboxRequest(
            enabled=enabled_override if enabled_override is not None else (self.enabled if self.enabled is not None else True),
            namespace_restrictions=namespace_override if namespace_override is not None else (self.namespace_restrictions if self.namespace_restrictions is not None else True),
            network_isolation=network_override if network_override is not None else (self.network_isolation if self.network_isolation is not None else False),
            filesystem_mode=filesystem_mode_override or self.filesystem_mode or FilesystemIsolationMode.WORKSPACE_ONLY,
            allowed_mounts=allowed_mounts_override if allowed_mounts_override is not None else list(self.allowed_mounts),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SandboxConfig:
        mode = d.get("filesystem_mode")
        fs_mode = None
        if mode:
            with suppress(ValueError):
                fs_mode = FilesystemIsolationMode(mode)
        return cls(
            enabled=d.get("enabled"),
            namespace_restrictions=d.get("namespace_restrictions"),
            network_isolation=d.get("network_isolation"),
            filesystem_mode=fs_mode,
            allowed_mounts=d.get("allowed_mounts", []),
        )


@dataclass
class SandboxRequest:
    enabled: bool = True
    namespace_restrictions: bool = True
    network_isolation: bool = False
    filesystem_mode: FilesystemIsolationMode = FilesystemIsolationMode.WORKSPACE_ONLY
    allowed_mounts: list[str] = field(default_factory=list)


@dataclass
class ContainerEnvironment:
    in_container: bool = False
    markers: list[str] = field(default_factory=list)


@dataclass
class SandboxStatus:
    enabled: bool = False
    requested: SandboxRequest = field(default_factory=SandboxRequest)
    supported: bool = False
    active: bool = False
    namespace_supported: bool = False
    namespace_active: bool = False
    network_supported: bool = False
    network_active: bool = False
    filesystem_mode: FilesystemIsolationMode = FilesystemIsolationMode.WORKSPACE_ONLY
    filesystem_active: bool = False
    allowed_mounts: list[str] = field(default_factory=list)
    in_container: bool = False
    container_markers: list[str] = field(default_factory=list)
    fallback_reason: str | None = None


@dataclass
class LinuxSandboxCommand:
    program: str
    args: list[str]
    env: list[tuple[str, str]]


def detect_container_environment() -> ContainerEnvironment:
    markers: list[str] = []

    if Path("/.dockerenv").exists():
        markers.append("/.dockerenv")
    if Path("/run/.containerenv").exists():
        markers.append("/run/.containerenv")

    for key, value in os.environ.items():
        normalized = key.lower()
        if normalized in ("container", "docker", "podman", "kubernetes_service_host") and value:
            markers.append(f"env:{key}={value}")

    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        for needle in ("docker", "containerd", "kubepods", "podman", "libpod"):
            if needle in cgroup:
                markers.append(f"/proc/1/cgroup:{needle}")
    except OSError:
        pass

    markers = sorted(set(markers))
    return ContainerEnvironment(in_container=bool(markers), markers=markers)


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_sandbox_status(config: SandboxConfig, cwd: Path) -> SandboxStatus:
    request = config.resolve_request()
    return resolve_sandbox_status_for_request(request, cwd)


def resolve_sandbox_status_for_request(request: SandboxRequest, cwd: Path) -> SandboxStatus:
    container = detect_container_environment()
    is_linux = sys.platform == "linux"
    namespace_supported = is_linux and _command_exists("unshare")
    network_supported = namespace_supported

    filesystem_active = request.enabled and request.filesystem_mode != FilesystemIsolationMode.OFF

    fallback_reasons: list[str] = []
    if request.enabled and request.namespace_restrictions and not namespace_supported:
        fallback_reasons.append("namespace isolation unavailable (requires Linux with `unshare`)")
    if request.enabled and request.network_isolation and not network_supported:
        fallback_reasons.append("network isolation unavailable (requires Linux with `unshare`)")
    if (
        request.enabled
        and request.filesystem_mode == FilesystemIsolationMode.ALLOW_LIST
        and not request.allowed_mounts
    ):
        fallback_reasons.append("filesystem allow-list requested without configured mounts")

    active = request.enabled and (
        (not request.namespace_restrictions or namespace_supported)
        and (not request.network_isolation or network_supported)
    )

    allowed_mounts = _normalize_mounts(request.allowed_mounts, cwd)

    return SandboxStatus(
        enabled=request.enabled,
        requested=request,
        supported=namespace_supported,
        active=active,
        namespace_supported=namespace_supported,
        namespace_active=request.enabled and request.namespace_restrictions and namespace_supported,
        network_supported=network_supported,
        network_active=request.enabled and request.network_isolation and network_supported,
        filesystem_mode=request.filesystem_mode,
        filesystem_active=filesystem_active,
        allowed_mounts=allowed_mounts,
        in_container=container.in_container,
        container_markers=container.markers,
        fallback_reason="; ".join(fallback_reasons) if fallback_reasons else None,
    )


def build_linux_sandbox_command(
    command: str, cwd: Path, status: SandboxStatus,
) -> LinuxSandboxCommand | None:
    if sys.platform != "linux" or not status.enabled:
        return None
    if not status.namespace_active and not status.network_active:
        return None

    args = [
        "--user", "--map-root-user",
        "--mount", "--ipc", "--pid", "--uts", "--fork",
    ]
    if status.network_active:
        args.append("--net")
    args.extend(["sh", "-lc", command])

    sandbox_home = cwd / ".sandbox-home"
    sandbox_tmp = cwd / ".sandbox-tmp"
    env: list[tuple[str, str]] = [
        ("HOME", str(sandbox_home)),
        ("TMPDIR", str(sandbox_tmp)),
        ("YUCODE_SANDBOX_FILESYSTEM_MODE", status.filesystem_mode.value),
        ("YUCODE_SANDBOX_ALLOWED_MOUNTS", ":".join(status.allowed_mounts)),
    ]
    path_val = os.environ.get("PATH", "")
    if path_val:
        env.append(("PATH", path_val))

    return LinuxSandboxCommand(program="unshare", args=args, env=env)


def _normalize_mounts(mounts: list[str], cwd: Path) -> list[str]:
    result = []
    for mount in mounts:
        p = Path(mount)
        if p.is_absolute():
            result.append(str(p))
        else:
            result.append(str(cwd / p))
    return result
