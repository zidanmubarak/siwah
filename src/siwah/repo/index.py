"""Repo intelligence: gitignore-aware files, symbols, and a budgeted repo map."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser

from ..paths import siwah_home

CODE_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
}
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".next",
    "coverage",
}
_CACHE_VERSION = 1
_INDEXES: dict[str, WorkspaceIndex] = {}
_INDEX_LOCK = threading.Lock()


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int
    exported: bool = True
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "exported": self.exported,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Symbol:
        return cls(
            name=str(value.get("name", "")),
            kind=str(value.get("kind", "symbol")),
            line=int(value.get("line", 0)),
            exported=bool(value.get("exported", True)),
            signature=str(value.get("signature", "")),
        )


def get_index(workspace: Path) -> WorkspaceIndex:
    key = str(workspace.resolve())
    with _INDEX_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = WorkspaceIndex(workspace)
            _INDEXES[key] = index
        return index


class WorkspaceIndex:
    def __init__(self, workspace: Path, cache_root: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        digest = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:20]
        self.cache_root = cache_root or (siwah_home() / "cache" / "repomap")
        self.cache_path = self.cache_root / f"{digest}.json"
        self._files: list[Path] | None = None
        self._files_at = 0.0
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._files = None

    def files(self) -> list[Path]:
        # A long-lived daemon must see files created by the agent; a permanent
        # memoized list goes stale after the first call.
        if self._files is not None and time.monotonic() - self._files_at < 2.0:
            return self._files
        files = self._git_files()
        if files is None:
            files = self._walk_files()
        self._files = [path for path in files if path.is_file()]
        self._files_at = time.monotonic()
        return self._files

    def _git_files(self) -> list[Path] | None:
        if shutil.which("git") is None or not (self.workspace / ".git").exists():
            return None
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self.workspace),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            return None
        names = [name for name in completed.stdout.decode("utf-8", "replace").split("\0") if name]
        return [self.workspace / name for name in names]

    def _walk_files(self) -> list[Path]:
        import os

        files: list[Path] = []
        for directory, dirnames, filenames in os.walk(self.workspace):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            for filename in filenames:
                files.append(Path(directory) / filename)
        return files

    def code_files(self) -> list[Path]:
        result: list[Path] = []
        for path in self.files():
            if path.suffix.lower() not in CODE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
            except OSError:
                continue
            result.append(path)
        return result

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------
    def symbols(
        self, *, path_filter: Callable[[Path], bool] | None = None
    ) -> dict[str, list[Symbol]]:
        with self._lock:
            self._load_cache()
            result: dict[str, list[Symbol]] = {}
            seen: set[str] = set()
            changed = False
            for path in self.code_files():
                relative = path.relative_to(self.workspace).as_posix()
                if path_filter is not None and not path_filter(path):
                    # Keep other sessions' cache entries, but never return or
                    # parse a file excluded by this caller's effective policy.
                    seen.add(relative)
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                seen.add(relative)
                cached = self._cache.get(relative)
                if (
                    isinstance(cached, dict)
                    and cached.get("mtime_ns") == stat.st_mtime_ns
                    and cached.get("size") == stat.st_size
                    and isinstance(cached.get("symbols"), list)
                ):
                    result[relative] = [
                        Symbol.from_dict(item)
                        for item in cached["symbols"]
                        if isinstance(item, dict)
                    ]
                    continue
                parsed = self._parse_symbols(path)
                result[relative] = parsed
                self._cache[relative] = {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "symbols": [symbol.to_dict() for symbol in parsed],
                }
                changed = True
            for relative in list(self._cache):
                if relative not in seen:
                    self._cache.pop(relative, None)
                    changed = True
            if changed:
                self._save_cache()
            return result

    def _load_cache(self) -> None:
        if self._cache:
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != _CACHE_VERSION:
            return
        files = payload.get("files")
        if isinstance(files, dict):
            self._cache = {
                str(name): value for name, value in files.items() if isinstance(value, dict)
            }

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            payload = {
                "version": _CACHE_VERSION,
                "workspace": str(self.workspace),
                "files": self._cache,
            }
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.cache_path)
        except OSError:
            return

    def _parse_symbols(self, path: Path) -> list[Symbol]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        language = _language(path)
        if language is not None:
            try:
                tree = Parser(language).parse(text.encode("utf-8"))
                return _tree_symbols(tree.root_node, path)
            except (OSError, ValueError):
                pass
        return _regex_symbols(path, text)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def repo_map(
        self,
        query: str = "",
        budget_tokens: int = 4000,
        *,
        path_filter: Callable[[Path], bool] | None = None,
    ) -> str:
        budget = max(256, min(int(budget_tokens), 32_000))
        char_budget = budget * 4
        query_tokens = _tokens(query)
        symbols = self.symbols(path_filter=path_filter)
        ranked: list[tuple[float, str, list[tuple[float, Symbol]]]] = []
        for relative, items in symbols.items():
            path_score = _path_score(relative, query_tokens)
            scored = sorted(
                ((_symbol_score(item, query_tokens), item) for item in items),
                key=lambda pair: pair[0],
                reverse=True,
            )
            best = scored[0][0] if scored else 0.0
            ranked.append((path_score + best + (1.0 if items else 0.0), relative, scored))
        ranked.sort(key=lambda item: item[0], reverse=True)

        lines: list[str] = []
        header = f"Repo map: {len(symbols)} files, {sum(len(v) for v in symbols.values())} symbols"
        lines.append(header)
        used = len(header) + 1
        included_files = 0
        included_symbols = 0
        for _, relative, scored in ranked:
            file_line = relative
            if used + len(file_line) + 1 > char_budget:
                break
            lines.append(file_line)
            used += len(file_line) + 1
            included_files += 1
            for symbol_score, symbol in scored[:12]:
                if symbol_score <= 0 and query_tokens:
                    continue
                signature = f"  {symbol.kind} {symbol.name}"
                if symbol.signature:
                    signature += f"  {symbol.signature}"
                signature += f"  :{symbol.line}"
                if used + len(signature) + 1 > char_budget:
                    break
                lines.append(signature)
                used += len(signature) + 1
                included_symbols += 1
        if included_files == 0:
            return "Repo map: no code files found"
        lines.append(f"... {included_files} files and {included_symbols} symbols shown")
        return "\n".join(lines)

    def find_symbols(
        self,
        query: str,
        limit: int = 50,
        *,
        path_filter: Callable[[Path], bool] | None = None,
        exact_name: bool = False,
    ) -> list[tuple[str, Symbol]]:
        tokens = _tokens(query)
        matches: list[tuple[float, str, Symbol]] = []
        for relative, items in self.symbols(path_filter=path_filter).items():
            path_score = _path_score(relative, tokens)
            for symbol in items:
                if exact_name and symbol.name.split(".")[-1] != query:
                    continue
                score = _symbol_score(symbol, tokens) + path_score
                if not tokens or score > 0:
                    matches.append((score, relative, symbol))
        matches.sort(key=lambda item: (item[0], item[1], item[2].line), reverse=True)
        return [(relative, symbol) for _, relative, symbol in matches[: max(1, limit)]]

    def find_refs(
        self,
        query: str,
        limit: int = 100,
        glob: str | None = None,
        *,
        path_filter: Callable[[Path], bool] | None = None,
    ) -> list[str]:
        if not query.strip():
            return []
        # A recursive external grep cannot enforce a per-file policy before
        # reading. Use the filtered in-process search for permission-scoped calls.
        matches = self._ripgrep(query) if path_filter is None else None
        if matches is None:
            matches = self._python_refs(query, path_filter=path_filter)
        results: list[str] = []
        for relative, line, content in matches:
            if glob and not _glob_match(relative, glob):
                continue
            results.append(f"{relative}:{line}: {content}")
            if len(results) >= max(1, limit):
                break
        return results

    def _ripgrep(self, query: str) -> list[tuple[str, int, str]] | None:
        executable = shutil.which("rg")
        if executable is None:
            return None
        completed = subprocess.run(
            [executable, "-n", "-F", "--no-heading", "--color", "never", query, "."],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            return None
        return _parse_grep(completed.stdout)

    def _python_refs(
        self, query: str, *, path_filter: Callable[[Path], bool] | None = None
    ) -> list[tuple[str, int, str]]:
        results: list[tuple[str, int, str]] = []
        for path in self.files():
            if path_filter is not None and not path_filter(path):
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            relative = path.relative_to(self.workspace).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    results.append((relative, number, line.strip()[:300]))
        return results


def _language(path: Path) -> Language | None:
    suffix = path.suffix.lower()
    try:
        if suffix in {".py", ".pyi"}:
            import tree_sitter_python

            return Language(tree_sitter_python.language())
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            import tree_sitter_javascript

            return Language(tree_sitter_javascript.language())
        if suffix in {".ts", ".tsx"}:
            import tree_sitter_typescript

            factory = (
                tree_sitter_typescript.language_tsx
                if suffix == ".tsx"
                else tree_sitter_typescript.language_typescript
            )
            return Language(factory())
    except (ImportError, OSError, ValueError):
        return None
    return None


def _tree_symbols(root: Node, path: Path) -> list[Symbol]:
    suffix = path.suffix.lower()
    result: list[Symbol] = []
    if suffix in {".py", ".pyi"}:
        _python_symbols(root, result, prefix="")
    else:
        _js_symbols(root, result, prefix="")
    return result


def _python_symbols(node: Node, result: list[Symbol], prefix: str) -> None:
    for child in node.children:
        if child.type == "decorated_definition":
            _python_symbols(child, result, prefix=prefix)
            continue
        if child.type in {"function_definition", "class_definition"}:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            kind = "class" if child.type == "class_definition" else "function"
            full_name = f"{prefix}.{name}" if prefix else name
            result.append(
                Symbol(
                    name=full_name,
                    kind=kind,
                    line=child.start_point[0] + 1,
                    exported=not name.startswith("_"),
                    signature=_signature(child),
                )
            )
            if child.type == "class_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    _python_methods(body, result, prefix=full_name)
        elif child.type == "block":
            _python_symbols(child, result, prefix=prefix)


def _python_methods(node: Node, result: list[Symbol], prefix: str) -> None:
    for child in node.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            result.append(
                Symbol(
                    name=f"{prefix}.{name}",
                    kind="method",
                    line=child.start_point[0] + 1,
                    exported=not name.startswith("_"),
                    signature=_signature(child),
                )
            )


def _js_symbols(node: Node, result: list[Symbol], prefix: str) -> None:
    for child in node.children:
        if child.type == "export_statement":
            _js_symbols(child, result, prefix=prefix)
            continue
        if child.type in {"function_declaration", "class_declaration"}:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            full_name = f"{prefix}.{name}" if prefix else name
            kind = "class" if child.type == "class_declaration" else "function"
            result.append(
                Symbol(
                    name=full_name,
                    kind=kind,
                    line=child.start_point[0] + 1,
                    exported=not name.startswith("_"),
                    signature=_signature(child),
                )
            )
            if child.type == "class_declaration":
                body = child.child_by_field_name("body")
                if body is not None:
                    _js_methods(body, result, prefix=full_name)
        elif child.type in {
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
        }:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            result.append(
                Symbol(
                    name=name,
                    kind="type",
                    line=child.start_point[0] + 1,
                    exported=not name.startswith("_"),
                    signature=_signature(child),
                )
            )
        elif child.type == "lexical_declaration":
            for declarator in child.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or value is None:
                    continue
                if value.type not in {"arrow_function", "function_expression", "function"}:
                    continue
                name = _text(name_node)
                result.append(
                    Symbol(
                        name=name,
                        kind="function",
                        line=declarator.start_point[0] + 1,
                        exported=not name.startswith("_"),
                        signature=_signature(declarator),
                    )
                )
        else:
            _js_symbols(child, result, prefix=prefix)


def _js_methods(node: Node, result: list[Symbol], prefix: str) -> None:
    for child in node.children:
        if child.type in {"method_definition", "method_signature"}:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            result.append(
                Symbol(
                    name=f"{prefix}.{name}",
                    kind="method",
                    line=child.start_point[0] + 1,
                    exported=not name.startswith("_"),
                    signature=_signature(child),
                )
            )
        elif child.type == "class_body" or child.type == "object_type":
            _js_methods(child, result, prefix=prefix)


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", "replace")


def _signature(node: Node) -> str:
    text = _text(node)
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return line[:120]


def _regex_symbols(path: Path, text: str) -> list[Symbol]:
    patterns = [
        (r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(", "function"),
        (r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:pub\s+)?(?:struct|enum|trait|interface)\s+([A-Za-z_]\w*)", "type"),
    ]
    result: list[Symbol] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            match = re.match(pattern, line)
            if match:
                name = match.group(1)
                result.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        line=line_number,
                        exported=not name.startswith("_"),
                        signature=line.strip()[:120],
                    )
                )
                break
    return result


def _tokens(value: str) -> set[str]:
    return {
        token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value) if len(token) >= 2
    }


def _name_tokens(value: str) -> list[str]:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        tokens.extend(
            token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", part)
        )
    return tokens


def _path_score(relative: str, query_tokens: set[str]) -> float:
    if not query_tokens:
        return max(0.0, 3.0 - len(Path(relative).parts) * 0.2)
    path_tokens = set(_name_tokens(relative))
    score = 0.0
    for token in query_tokens:
        if token in path_tokens:
            score += 3.0
        elif any(token in candidate for candidate in path_tokens):
            score += 1.0
    return score


def _symbol_score(symbol: Symbol, query_tokens: set[str]) -> float:
    score = 1.0 if symbol.exported else 0.0
    if symbol.kind in {"class", "function", "type"}:
        score += 0.5
    if not query_tokens:
        return score
    name_tokens = set(_name_tokens(symbol.name))
    for token in query_tokens:
        if token in name_tokens:
            score += 6.0
        elif any(candidate.startswith(token) for candidate in name_tokens):
            score += 3.0
        elif token in symbol.name.lower() or token in symbol.signature.lower():
            score += 1.0
    return score


def _glob_match(relative: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(relative, pattern) or fnmatch(Path(relative).name, pattern)


def _parse_grep(output: str) -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            number = int(parts[1])
        except ValueError:
            continue
        path = parts[0].replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        results.append((path, number, parts[2].strip()[:300]))
    return results


__all__ = ["Symbol", "WorkspaceIndex", "get_index"]
