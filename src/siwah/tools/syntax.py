"""Syntax validation before writing edited files.

Python, JavaScript, and TypeScript use tree-sitter. JSON and TOML use the
standard library. Unknown languages are not rejected; the caller can still rely
on the project verifier.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tree_sitter import Language, Node, Parser

_LANGUAGES: dict[str, Language] = {}


def _language(path: Path) -> Language | None:
    suffix = path.suffix.lower()
    if suffix == ".py":
        import tree_sitter_python

        key = "python"
        if key not in _LANGUAGES:
            _LANGUAGES[key] = Language(tree_sitter_python.language())
        return _LANGUAGES[key]
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        import tree_sitter_javascript

        key = "javascript"
        if key not in _LANGUAGES:
            _LANGUAGES[key] = Language(tree_sitter_javascript.language())
        return _LANGUAGES[key]
    if suffix in {".ts", ".tsx"}:
        import tree_sitter_typescript

        key = "tsx" if suffix == ".tsx" else "typescript"
        if key not in _LANGUAGES:
            factory = (
                tree_sitter_typescript.language_tsx
                if suffix == ".tsx"
                else tree_sitter_typescript.language_typescript
            )
            _LANGUAGES[key] = Language(factory())
        return _LANGUAGES[key]
    return None


def _first_error(node: Node) -> Node | None:
    if node.type == "ERROR" or node.is_missing:
        return node
    for child in node.children:
        found = _first_error(child)
        if found is not None:
            return found
    return None


def _tree_sitter_error(path: Path, content: str) -> str | None:
    language = _language(path)
    if language is None:
        return None
    parser = Parser(language)
    tree = parser.parse(content.encode("utf-8"))
    if not tree.root_node.has_error:
        return None
    node = _first_error(tree.root_node) or tree.root_node
    line = node.start_point[0] + 1
    snippet = (node.text or b"").decode("utf-8", errors="replace")[:160].replace("\n", " ")
    kind = "missing syntax" if node.is_missing else "syntax error"
    return f"{path.name}: {kind} at line {line}" + (f" near {snippet!r}" if snippet else "")


def validate_syntax(path: Path, content: str) -> str | None:
    """Return a human-readable error, or None when the content is valid."""

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return f"{path.name}: JSON error at line {exc.lineno}: {exc.msg}"
        return None
    if suffix == ".toml":
        try:
            tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            return f"{path.name}: TOML error: {exc}"
        return None
    try:
        return _tree_sitter_error(path, content)
    except (ImportError, OSError, ValueError):
        # A missing grammar must not make editing impossible.
        return None


__all__ = ["validate_syntax"]
