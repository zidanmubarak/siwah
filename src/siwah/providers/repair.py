"""Tool-call repair helpers.

Models sometimes stream truncated JSON, wrap it in markdown, or emit tool calls
inside text. These helpers make a best effort and never execute a call with
unparsed arguments.
"""

from __future__ import annotations

import json
import re
from typing import Any


class ToolArgumentError(ValueError):
    pass


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None or not raw.strip():
        return {}
    candidates = [raw.strip(), _strip_fence(raw)]
    for candidate in candidates:
        parsed = _try_json(candidate)
        if parsed is not None:
            return parsed
    cleaned = _remove_trailing_commas(candidates[-1])
    parsed = _try_json(cleaned)
    if parsed is not None:
        return parsed
    sliced = _slice_object(cleaned)
    if sliced:
        parsed = _try_json(_remove_trailing_commas(sliced))
        if parsed is not None:
            return parsed
    raise ToolArgumentError(f"Could not parse tool arguments: {raw[:200]}")


def extract_text_tool_calls(
    text: str, allowed_names: set[str] | None = None
) -> list[dict[str, Any]]:
    """Find JSON objects that look like `{name, arguments}` in assistant text."""
    found: list[dict[str, Any]] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        found.extend(_extract_from_json(match.group(1), allowed_names))
    for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+"[^{}]*\}', text):
        found.extend(_extract_from_json(match.group(0), allowed_names))
    # De-duplicate by name + serialized arguments.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in found:
        key = json.dumps(call, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(call)
    return unique


def _extract_from_json(value: str, allowed_names: set[str] | None) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool")
        if not isinstance(name, str):
            continue
        if allowed_names is not None and name not in allowed_names:
            continue
        arguments = item.get("arguments", item.get("args", {}))
        if isinstance(arguments, str):
            try:
                arguments = parse_tool_arguments(arguments)
            except ToolArgumentError:
                continue
        if not isinstance(arguments, dict):
            continue
        calls.append({"name": name, "arguments": arguments})
    return calls


def _try_json(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _strip_fence(value: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL)
    return match.group(1) if match else value


def _remove_trailing_commas(value: str) -> str:
    """Drop syntactically trailing commas without touching string contents."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def has_trailing_object(value: str) -> bool:
    """True when a second JSON object follows the first one."""

    sliced = _slice_object(value)
    if sliced is None:
        return False
    end = value.find(sliced) + len(sliced)
    return "{" in value[end:]


def _slice_object(value: str) -> str | None:
    start = value.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        character = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return None
