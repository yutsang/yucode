"""In-memory LSP registry for language server dispatch.

Port of ``claw-code-main/rust/crates/runtime/src/lsp_client.rs``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LspAction(str, Enum):
    DIAGNOSTICS = "diagnostics"
    HOVER = "hover"
    DEFINITION = "definition"
    REFERENCES = "references"
    COMPLETION = "completion"
    SYMBOLS = "symbols"
    FORMAT = "format"

    @classmethod
    def from_str(cls, s: str) -> LspAction | None:
        aliases: dict[str, LspAction] = {
            "diagnostics": cls.DIAGNOSTICS,
            "hover": cls.HOVER,
            "definition": cls.DEFINITION,
            "go_to_definition": cls.DEFINITION,
            "goto_definition": cls.DEFINITION,
            "references": cls.REFERENCES,
            "find_references": cls.REFERENCES,
            "completion": cls.COMPLETION,
            "completions": cls.COMPLETION,
            "symbols": cls.SYMBOLS,
            "document_symbols": cls.SYMBOLS,
            "format": cls.FORMAT,
            "formatting": cls.FORMAT,
        }
        return aliases.get(s.lower().strip())


class LspServerStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STARTING = "starting"
    ERROR = "error"


@dataclass
class LspDiagnostic:
    path: str
    line: int
    character: int
    severity: str
    message: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "character": self.character,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
        }


@dataclass
class LspServerState:
    language: str
    status: LspServerStatus = LspServerStatus.DISCONNECTED
    root_path: str = ""
    capabilities: list[str] = field(default_factory=list)
    diagnostics: list[LspDiagnostic] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "status": self.status.value,
            "root_path": self.root_path,
            "capabilities": list(self.capabilities),
            "diagnostic_count": len(self.diagnostics),
        }


_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}


class LspRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, LspServerState] = {}

    def register(
        self,
        language: str,
        status: LspServerStatus = LspServerStatus.CONNECTED,
        root_path: str = "",
        capabilities: list[str] | None = None,
    ) -> None:
        with self._lock:
            self._servers[language] = LspServerState(
                language=language,
                status=status,
                root_path=root_path,
                capabilities=list(capabilities or []),
            )

    def get(self, language: str) -> LspServerState | None:
        with self._lock:
            return self._servers.get(language)

    def find_server_for_path(self, path: str) -> LspServerState | None:
        import os
        _, ext = os.path.splitext(path)
        lang = _EXTENSION_LANGUAGE.get(ext.lower())
        if lang:
            return self.get(lang)
        return None

    def list_servers(self) -> list[LspServerState]:
        with self._lock:
            return list(self._servers.values())

    def add_diagnostics(self, language: str, diagnostics: list[LspDiagnostic]) -> None:
        with self._lock:
            server = self._servers.get(language)
            if server is None:
                raise KeyError(f"No LSP server registered for `{language}`")
            server.diagnostics.extend(diagnostics)

    def get_diagnostics(self, path: str = "") -> list[LspDiagnostic]:
        with self._lock:
            if not path:
                result: list[LspDiagnostic] = []
                for server in self._servers.values():
                    result.extend(server.diagnostics)
                return result
            server = self.find_server_for_path(path)
            if server is None:
                return []
            return [d for d in server.diagnostics if d.path == path or not d.path]

    def clear_diagnostics(self, language: str) -> None:
        with self._lock:
            server = self._servers.get(language)
            if server is None:
                raise KeyError(f"No LSP server registered for `{language}`")
            server.diagnostics.clear()

    def disconnect(self, language: str) -> LspServerState | None:
        with self._lock:
            return self._servers.pop(language, None)

    def dispatch(
        self,
        action: str,
        path: str = "",
        line: int = 0,
        character: int = 0,
        query: str = "",
    ) -> dict[str, Any]:
        lsp_action = LspAction.from_str(action)
        if lsp_action is None:
            return {"error": f"Unknown LSP action: {action}"}

        if lsp_action == LspAction.DIAGNOSTICS:
            diagnostics = self.get_diagnostics(path)
            return {"diagnostics": [d.to_dict() for d in diagnostics], "count": len(diagnostics)}

        server = self.find_server_for_path(path) if path else None
        if server is None:
            with self._lock:
                servers = list(self._servers.values())
            if not servers:
                return {"error": "No LSP servers registered"}
            server = servers[0]

        if server.status != LspServerStatus.CONNECTED:
            return {"error": f"LSP server for `{server.language}` is {server.status.value}"}

        return {
            "action": lsp_action.value,
            "language": server.language,
            "path": path,
            "line": line,
            "character": character,
            "result": f"LSP {lsp_action.value} result placeholder",
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._servers)


_global_lsp_registry: LspRegistry | None = None
_global_lock = threading.Lock()


def global_lsp_registry() -> LspRegistry:
    global _global_lsp_registry
    with _global_lock:
        if _global_lsp_registry is None:
            _global_lsp_registry = LspRegistry()
        return _global_lsp_registry
