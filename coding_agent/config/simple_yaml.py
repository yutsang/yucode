from __future__ import annotations

from typing import Any


class YamlError(ValueError):
    pass


def load_yaml(text: str) -> Any:
    lines = _preprocess(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise YamlError("Unexpected trailing YAML content")
    return value


def dump_yaml(value: Any) -> str:
    return _dump_value(value, 0).rstrip() + "\n"


def _preprocess(text: str) -> list[tuple[int, str]]:
    processed: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        without_comment = _strip_comment(raw_line).rstrip()
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        processed.append((indent, without_comment.strip()))
    return processed


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    current_indent, current_text = lines[index]
    if current_indent != indent:
        raise YamlError(f"Unexpected indentation near `{current_text}`")
    if current_text.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, current_text = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise YamlError(f"Unexpected indentation near `{current_text}`")
        if current_text.startswith("- "):
            break
        if ":" not in current_text:
            raise YamlError(f"Expected `key: value` near `{current_text}`")
        key, raw_value = current_text.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        index += 1
        if raw_value:
            result[key] = _parse_scalar(raw_value)
            continue
        if index >= len(lines) or lines[index][0] < indent:
            result[key] = {}
            continue
        next_indent = lines[index][0]
        if next_indent == indent and lines[index][1].startswith("- "):
            value, index = _parse_list(lines, index, indent)
            result[key] = value
            continue
        if next_indent <= indent:
            result[key] = {}
            continue
        value, index = _parse_block(lines, index, next_indent)
        result[key] = value
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        current_indent, current_text = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not current_text.startswith("- "):
            break
        item_text = current_text[2:].strip()
        index += 1
        if not item_text:
            if index >= len(lines) or lines[index][0] <= indent:
                result.append(None)
                continue
            value, index = _parse_block(lines, index, lines[index][0])
            result.append(value)
            continue
        if ":" in item_text and not item_text.startswith(("'", '"', "[", "{")):
            key, raw_value = item_text.split(":", 1)
            item: dict[str, Any] = {}
            key = key.strip()
            raw_value = raw_value.strip()
            if raw_value:
                item[key] = _parse_scalar(raw_value)
            else:
                item[key] = {}
            if index < len(lines) and lines[index][0] > indent:
                nested, index = _parse_map(lines, index, lines[index][0])
                item.update(nested)
            result.append(item)
            continue
        result.append(_parse_scalar(item_text))
    return result, index


def _parse_scalar(value: str) -> Any:
    if value in {"[]", "[ ]"}:
        return []
    if value in {"{}", "{ }"}:
        return {}
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _dump_value(value: Any, indent: int) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(f"{prefix}{key}: {_inline_empty(item)}")
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.append(_dump_value(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_format_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(f"{prefix}- {_inline_empty(item)}")
                else:
                    nested = _dump_value(item, indent + 4).splitlines()
                    if isinstance(item, dict) and nested:
                        first = nested[0].strip()
                        rest = nested[1:]
                        lines.append(f"{prefix}- {first}")
                        lines.extend(f"{' ' * (indent + 2)}{line.strip()}" for line in rest)
                    else:
                        lines.append(f"{prefix}-")
                        lines.extend(nested)
            else:
                lines.append(f"{prefix}- {_format_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{_format_scalar(value)}"


def _format_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in [":", "#", '"', "'", "[", "]", "{", "}"]):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _inline_empty(value: Any) -> str:
    return "[]" if isinstance(value, list) else "{}"
