"""Bounded JSON and safe, data-only YAML import/export for strategy documents."""
from __future__ import annotations

import json
import re
from typing import Any

from .schema import MAX_CONFIG_BYTES

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def load_json(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds size limit")
    value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text in {"null", "Null", "NULL", "~"}:
        return None
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if text.startswith(("[", "{")):
        return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")), object_pairs_hook=_unique_object)
    if text.startswith(('"', "'")):
        if len(text) < 2 or text[-1] != text[0]:
            raise ValueError("unterminated quoted scalar")
        if text[0] == '"':
            return json.loads(text)
        return text[1:-1].replace("''", "'")
    if _NUMBER.fullmatch(text):
        return float(text) if any(character in text for character in ".eE") else int(text)
    if any(token in text for token in (" #", ": ", "&", "*", "!", "|", ">")):
        raise ValueError("unsupported or unsafe YAML scalar; quote it explicitly")
    return text


def load_yaml(text: str) -> dict[str, Any]:
    """Parse the intentionally small YAML subset emitted by :func:`dump_yaml`.

    Tags, anchors, aliases, merge keys, block scalars, duplicate keys and custom
    objects are unsupported, so importing configuration can never construct code.
    """
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds size limit")
    records: list[tuple[int, str, int]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
            continue
        if "\t" in raw:
            raise ValueError(f"line {line_number}: tabs are forbidden")
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ValueError(f"line {line_number}: indentation must use pairs of spaces")
        records.append((indent, raw.strip(), line_number))
    if not records:
        raise ValueError("empty YAML document")

    def parse_block(position: int, indent: int) -> tuple[Any, int]:
        if position >= len(records) or records[position][0] != indent:
            raise ValueError("invalid indentation")
        is_list = records[position][1].startswith("- ") or records[position][1] == "-"
        container: Any = [] if is_list else {}
        while position < len(records):
            current_indent, content, line_number = records[position]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(f"line {line_number}: unexpected indentation")
            if is_list:
                if not (content.startswith("- ") or content == "-"):
                    break
                remainder = content[1:].strip()
                if not remainder:
                    value, position = parse_block(position + 1, indent + 2)
                    container.append(value)
                    continue
                if ":" in remainder:
                    key, raw_value = remainder.split(":", 1)
                    if not _KEY.fullmatch(key) or key == "<<":
                        raise ValueError(f"line {line_number}: invalid mapping key")
                    item: dict[str, Any] = {}
                    if raw_value.strip():
                        item[key] = _scalar(raw_value)
                        position += 1
                    else:
                        value, position = parse_block(position + 1, indent + 2)
                        item[key] = value
                    while position < len(records) and records[position][0] == indent + 2 and not records[position][1].startswith("-"):
                        child_content = records[position][1]
                        child_line = records[position][2]
                        if ":" not in child_content:
                            raise ValueError(f"line {child_line}: expected key: value")
                        child_key, child_raw = child_content.split(":", 1)
                        if not _KEY.fullmatch(child_key) or child_key in item or child_key == "<<":
                            raise ValueError(f"line {child_line}: invalid or duplicate mapping key")
                        if child_raw.strip():
                            item[child_key] = _scalar(child_raw)
                            position += 1
                        else:
                            value, position = parse_block(position + 1, indent + 4)
                            item[child_key] = value
                    container.append(item)
                    continue
                container.append(_scalar(remainder))
                position += 1
                continue
            if content.startswith("-") or ":" not in content:
                raise ValueError(f"line {line_number}: expected key: value")
            key, raw_value = content.split(":", 1)
            if not _KEY.fullmatch(key) or key == "<<" or key in container:
                raise ValueError(f"line {line_number}: invalid or duplicate mapping key")
            if raw_value.strip():
                container[key] = _scalar(raw_value)
                position += 1
            else:
                if position + 1 >= len(records) or records[position + 1][0] <= indent:
                    container[key] = None
                    position += 1
                else:
                    value, position = parse_block(position + 1, indent + 2)
                    container[key] = value
        return container, position

    value, final = parse_block(0, records[0][0])
    if final != len(records) or not isinstance(value, dict):
        raise ValueError("YAML root must be one mapping")
    return value


def load_document(text: str, format_name: str | None = None) -> dict[str, Any]:
    selected = format_name.lower() if format_name else ("json" if text.lstrip().startswith("{") else "yaml")
    if selected == "json":
        return load_json(text)
    if selected in {"yaml", "yml"}:
        return load_yaml(text)
    raise ValueError("format must be json or yaml")


def dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=False, ensure_ascii=False, allow_nan=False) + "\n"


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, allow_nan=False)
    return json.dumps(str(value), ensure_ascii=False)


def dump_yaml(value: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit(node: Any, indent: int) -> None:
        prefix = " " * indent
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, dict) and not item:
                    lines.append(f"{prefix}{key}: {{}}")
                elif isinstance(item, list) and not item:
                    lines.append(f"{prefix}{key}: []")
                elif isinstance(item, (dict, list)):
                    lines.append(f"{prefix}{key}:")
                    emit(item, indent + 2)
                else:
                    lines.append(f"{prefix}{key}: {_dump_scalar(item)}")
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and not item:
                    lines.append(f"{prefix}- {{}}")
                elif isinstance(item, list) and not item:
                    lines.append(f"{prefix}- []")
                elif isinstance(item, dict):
                    first = True
                    for key, child in item.items():
                        marker = "- " if first else "  "
                        if isinstance(child, (dict, list)):
                            lines.append(f"{prefix}{marker}{key}:")
                            emit(child, indent + 4)
                        else:
                            lines.append(f"{prefix}{marker}{key}: {_dump_scalar(child)}")
                        first = False
                elif isinstance(item, list):
                    lines.append(f"{prefix}-")
                    emit(item, indent + 2)
                else:
                    lines.append(f"{prefix}- {_dump_scalar(item)}")
        else:
            raise TypeError("YAML document root must be structured")

    emit(value, 0)
    return "\n".join(lines) + "\n"
