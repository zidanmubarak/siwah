"""Detect duplicate and conflicting private memory entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory import _atomic_write, project_memory_path, read_memory, user_memory_path

_ENTRY_SPLIT = re.compile(r"\n\s*\n")
_KEY = re.compile(r"^(?:[-*]\s*)?([A-Za-z][\w ./-]{1,40})\s*[:=]\s*(.+)$")


@dataclass(frozen=True)
class MemoryFinding:
    kind: str
    source: str
    path: str
    entry: str
    other: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
            "entry": self.entry,
            "other": self.other,
        }


def _entries(text: str) -> list[str]:
    return [entry.strip() for entry in _ENTRY_SPLIT.split(text) if entry.strip()]


def _normalized(entry: str) -> str:
    return re.sub(r"\s+", " ", entry).strip().casefold()


def _key_value(entry: str) -> tuple[str, str] | None:
    first = entry.splitlines()[0].strip()
    match = _KEY.match(first)
    if match is None:
        return None
    return match.group(1).casefold(), match.group(2).strip()


def review_memory(workspace: Path) -> list[MemoryFinding]:
    snapshot = read_memory(workspace)
    sources = (
        ("user", user_memory_path(), snapshot.user.content),
        ("project", project_memory_path(workspace), snapshot.project.content),
        ("quick", user_memory_path().with_name("quick.md"), snapshot.quick.content),
    )
    findings: list[MemoryFinding] = []
    seen: dict[str, tuple[str, str]] = {}
    keys: dict[str, tuple[str, str, str]] = {}
    for source, path, content in sources:
        for entry in _entries(content):
            normalized = _normalized(entry)
            previous = seen.get(normalized)
            if previous is not None:
                findings.append(
                    MemoryFinding(
                        kind="duplicate",
                        source=source,
                        path=str(path),
                        entry=entry[:1_000],
                        other=f"{previous[0]}: {previous[1]}",
                    )
                )
            else:
                seen[normalized] = (source, entry[:1_000])
            pair = _key_value(entry)
            if pair is not None:
                key, value = pair
                prior = keys.get(key)
                if prior is not None and _normalized(prior[1]) != _normalized(value):
                    findings.append(
                        MemoryFinding(
                            kind="conflict",
                            source=source,
                            path=str(path),
                            entry=entry[:1_000],
                            other=f"{prior[0]}: {prior[2]}",
                        )
                    )
                elif prior is None:
                    keys[key] = (source, value, entry[:1_000])
    return findings


def dedupe_memory(workspace: Path, *, apply: bool = False) -> dict[str, Any]:
    """Remove exact duplicate entries, preserving the first occurrence."""

    snapshot = read_memory(workspace)
    paths = {
        "user": user_memory_path(),
        "project": project_memory_path(workspace),
        "quick": user_memory_path().with_name("quick.md"),
    }
    contents = {
        "user": snapshot.user.content,
        "project": snapshot.project.content,
        "quick": snapshot.quick.content,
    }
    removed: list[dict[str, str]] = []
    seen: set[str] = set()
    updated: dict[str, str] = {}
    for source in ("user", "project", "quick"):
        kept: list[str] = []
        for entry in _entries(contents[source]):
            normalized = _normalized(entry)
            if normalized in seen:
                removed.append({"source": source, "entry": entry[:500]})
                continue
            seen.add(normalized)
            kept.append(entry)
        updated[source] = "\n\n".join(kept).rstrip() + ("\n" if kept else "")
    if apply:
        for source, text in updated.items():
            if text != contents[source]:
                _atomic_write(paths[source], text)
    return {
        "apply": apply,
        "removed": removed,
        "changed": sum(1 for source in updated if updated[source] != contents[source]),
    }


__all__ = ["MemoryFinding", "dedupe_memory", "review_memory"]
