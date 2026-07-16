from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


def notebook_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec("edit_notebook_cell", "Edit or create a notebook cell by index.",
                     {"type": "object", "properties": {"path": {"type": "string"}, "cell_index": {"type": "integer"}, "new_source": {"type": "string"}}, "required": ["path", "cell_index", "new_source"]},
                     "workspace-write", RiskLevel.MEDIUM),
            lambda args: _edit_notebook_cell(registry, args),
        ),
    ]


def _edit_notebook_cell(registry: ToolRegistry, args: dict[str, Any]) -> str:
    path = registry._resolve_path(str(args["path"]))
    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells = notebook.setdefault("cells", [])
    index = int(args["cell_index"])
    while len(cells) <= index:
        cells.append({"cell_type": "code", "metadata": {}, "source": [], "outputs": [], "execution_count": None})
    source = str(args["new_source"])
    cells[index]["source"] = [line + "\n" for line in source.splitlines()] or [""]
    path.write_text(json.dumps(notebook, indent=2) + "\n", encoding="utf-8")
    return f"Updated notebook cell {index} in {path}"
