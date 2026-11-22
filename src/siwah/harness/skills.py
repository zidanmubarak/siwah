"""SKILL.md discovery, validation, and management."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..paths import harness_dir
from ..plugins import plugin_skill_roots
from .memory import secret_marker

MAX_DESCRIPTION = 500
MAX_SKILL_BODY = 64_000
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    path: Path


def skills_dir() -> Path:
    path = harness_dir() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_skills_dir(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".siwah" / "skills"


def _skill_roots(workspace: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    if workspace is not None:
        roots.append(project_skills_dir(workspace))
    roots.extend(plugin_skill_roots())
    roots.append(skills_dir())
    return roots


def validate_name(name: str) -> str:
    value = name.strip()
    if not SKILL_NAME.fullmatch(value):
        raise ValueError("skill name must be lowercase letters, digits, and hyphens (max 64 chars)")
    return value


def skill_path(name: str, workspace: Path | None = None) -> Path:
    clean = validate_name(name)
    if workspace is not None:
        for root in _skill_roots(workspace):
            path = root / clean / "SKILL.md"
            if path.is_file():
                return path
    return skills_dir() / clean / "SKILL.md"


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
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        value = raw.strip()
        if value in {">", "|", ">-", "|-", ">+", "|+"}:
            block: list[str] = []
            while index < end and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                block.append(lines[index].strip())
                index += 1
            if value.startswith(">"):
                value = " ".join(part for part in block if part)
            else:
                value = "\n".join(block).strip("\n")
            metadata[key] = value
            continue
        if value.startswith('"') and value.endswith('"'):
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        metadata[key] = value
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return metadata, body


def _render(name: str, description: str, content: str, *, base: str | None = None) -> str:
    metadata, _ = _parse_frontmatter(base) if base else ({}, "")
    lines = ["---", f"name: {json.dumps(name)}", f"description: {json.dumps(description)}"]
    for key, value in metadata.items():
        if key in {"name", "description"}:
            continue
        if "\n" in value:
            lines.append(f"{key}: |")
            lines.extend(f"  {part}" for part in value.splitlines())
        else:
            lines.append(f"{key}: {json.dumps(value)}")
    lines.extend(["---", "", content.strip()])
    return "\n".join(lines) + "\n"


def _summary(path: Path, text: str) -> SkillSummary | None:
    try:
        name = validate_name(path.parent.name)
    except ValueError:
        return None
    metadata, body = _parse_frontmatter(text)
    description = metadata.get("description", "").strip()
    if not description:
        description = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return SkillSummary(name=name, description=description[:MAX_DESCRIPTION])


def list_skills(workspace: Path | None = None) -> list[SkillSummary]:
    skills: list[SkillSummary] = []
    seen: set[str] = set()
    for root in _skill_roots(workspace):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            path = directory / "SKILL.md"
            if not directory.is_dir() or not path.is_file():
                continue
            try:
                summary = _summary(path, path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if summary and summary.name not in seen:
                seen.add(summary.name)
                skills.append(summary)
    return skills


def read_skill(name: str, workspace: Path | None = None) -> Skill:
    clean = validate_name(name)
    for root in _skill_roots(workspace):
        path = root / clean / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = _parse_frontmatter(text)
        description = metadata.get("description", "").strip()
        if not description:
            description = next((line.strip() for line in body.splitlines() if line.strip()), "")
        return Skill(name=clean, description=description, content=body, path=path)
    raise FileNotFoundError(f"Skill not found: {name}")


def _validate_content(name: str, description: str, content: str) -> None:
    validate_name(name)
    if not description.strip():
        raise ValueError("skill description is required")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"skill description exceeds {MAX_DESCRIPTION} characters")
    if not content.strip():
        raise ValueError("skill content is empty")
    if len(content) > MAX_SKILL_BODY:
        raise ValueError(f"skill content exceeds {MAX_SKILL_BODY} characters")
    marker = secret_marker(description + "\n" + content)
    if marker:
        raise ValueError(f"refusing to store a {marker}")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _duplicate_content(name: str, content: str) -> str | None:
    target = _content_hash(content)
    for summary in list_skills():
        if summary.name == name:
            continue
        try:
            other = read_skill(summary.name)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if _content_hash(other.content) == target:
            return summary.name
    return None


def _write(
    path: Path, description: str, content: str, name: str, *, base: str | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _render(name, description, content, base=base)
    fd, temp_name = tempfile.mkstemp(prefix=".SKILL.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        with contextlib.suppress(OSError):
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def create_skill(name: str, description: str, content: str, *, overwrite: bool = False) -> Skill:
    clean = validate_name(name)
    _validate_content(clean, description, content)
    if skill_path(clean).exists() and not overwrite:
        raise ValueError(f"Skill already exists: {clean}")
    duplicate = _duplicate_content(clean, content)
    if duplicate:
        raise ValueError(f"Skill content duplicates '{duplicate}'")
    _write(skill_path(clean), description, content, clean)
    return read_skill(clean)


def update_skill(
    name: str,
    *,
    description: str | None = None,
    content: str | None = None,
    workspace: Path | None = None,
) -> Skill:
    path = skill_path(name, workspace)
    if not path.is_file():
        raise FileNotFoundError(f"Skill not found: {name}")
    base = path.read_text(encoding="utf-8")
    current = read_skill(name, workspace)
    next_description = description if description is not None else current.description
    next_content = content if content is not None else current.content
    _validate_content(name, next_description, next_content)
    duplicate = _duplicate_content(name, next_content)
    if duplicate:
        raise ValueError(f"Skill content duplicates '{duplicate}'")
    _write(path, next_description, next_content, name, base=base)
    return read_skill(name, workspace)


def delete_skill(name: str, workspace: Path | None = None) -> bool:
    directory = skill_path(name, workspace).parent
    if not directory.is_dir():
        return False
    shutil.rmtree(directory)
    return True


MAX_INDEX_CHARS = 8_000
MAX_INDEX_DESCRIPTION = 120


def skill_index_text(workspace: Path | None = None) -> str:
    skills = list_skills(workspace)
    if not skills:
        return ""
    lines = [
        "## Available skills",
        "Call skill_view with a name to load its full instructions.",
    ]
    lines.extend(f"- {skill.name}: {skill.description[:MAX_INDEX_DESCRIPTION]}" for skill in skills)
    text = "\n".join(lines)
    if len(text) > MAX_INDEX_CHARS:
        text = (
            text[:MAX_INDEX_CHARS].rstrip()
            + f"\n... {len(skills)} skills truncated; use skill_list"
        )
    return text
