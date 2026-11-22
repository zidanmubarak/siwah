"""Custom slash commands stored in harness commands/*.md."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from .paths import harness_dir
from .plugins import plugin_command_roots


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    body: str
    path: Path

    def render(self, arguments: str) -> str:
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", arguments)
        if arguments:
            return self.body.rstrip() + "\n\n" + arguments
        return self.body


def commands_dir() -> Path:
    path = harness_dir() / "commands"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_commands_dir(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".siwah" / "commands"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        value = raw.strip()
        if value.startswith('"') and value.endswith('"'):
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        metadata[key.strip()] = value
    return metadata, "\n".join(lines[end + 1 :]).lstrip("\n")


def list_commands(root: Path | str | None = None) -> list[CommandSpec]:
    base = Path(root) if root is not None else commands_dir()
    if not base.is_dir():
        return []
    commands: list[CommandSpec] = []
    for path in sorted(base.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata, body = _parse_frontmatter(text)
        name = metadata.get("name", path.stem).strip().lstrip("/")
        if not name:
            continue
        description = metadata.get("description", "").strip()
        if not description:
            description = next((line.strip() for line in body.splitlines() if line.strip()), name)
        commands.append(
            CommandSpec(name=name, description=description[:500], body=body.strip(), path=path)
        )
    return commands


def list_commands_for(workspace: Path | None = None) -> list[CommandSpec]:
    roots: list[Path] = []
    if workspace is not None:
        roots.append(project_commands_dir(workspace))
    roots.extend(plugin_command_roots())
    roots.append(commands_dir())
    commands: list[CommandSpec] = []
    seen: set[str] = set()
    for root in roots:
        for command in list_commands(root):
            if command.name in seen:
                continue
            seen.add(command.name)
            commands.append(command)
    return commands


def find_command(name: str, root: Path | str | None = None) -> CommandSpec | None:
    clean = name.strip().lstrip("/")
    return next((command for command in list_commands(root) if command.name == clean), None)


__all__ = [
    "CommandSpec",
    "commands_dir",
    "find_command",
    "list_commands",
    "list_commands_for",
    "project_commands_dir",
]
