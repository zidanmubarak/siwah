"""Declarative plugin bundles.

A plugin is a directory (or zip) with ``plugin.toml`` and optional
``skills/``, ``commands/``, ``subagents/``, ``hooks/``, and ``mcp.json``.
Siwah never imports Python code from a plugin; installing a plugin only adds
harness data and MCP server definitions. Hooks still execute their configured
commands, so installing a plugin is a trust decision.

Example::

    plugin.toml
    skills/review/SKILL.md
    commands/review.md
    subagents/reviewer.md
    hooks/format.json
    mcp.json
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import siwah_home

PLUGIN_MANIFEST = "plugin.toml"
PLUGIN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PLUGIN_FILES = 5_000
MAX_PLUGIN_BYTES = 20 * 1024 * 1024
_IGNORED_NAMES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginInfo:
    name: str
    version: str
    description: str
    path: Path
    enabled: bool = True
    valid: bool = True
    error: str | None = None


def plugins_dir() -> Path:
    path = siwah_home() / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_name(value: str) -> str:
    name = value.strip().lower()
    if not PLUGIN_NAME.fullmatch(name):
        raise PluginError(
            "plugin name must be lowercase letters, digits, and hyphens (max 64 chars)"
        )
    return name


def _read_manifest(path: Path) -> dict[str, Any]:
    manifest = path / PLUGIN_MANIFEST
    try:
        payload = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginError(f"plugin manifest not found: {manifest}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"cannot read {manifest}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PluginError(f"{manifest} must contain a TOML table")
    return payload


def _manifest_info(path: Path) -> PluginInfo:
    payload = _read_manifest(path)
    fallback = path.name
    name = _validate_name(str(payload.get("name", fallback)))
    version = str(payload.get("version", "0.0.0")).strip() or "0.0.0"
    description = str(payload.get("description", "")).strip()
    return PluginInfo(
        name=name,
        version=version,
        description=description,
        path=path,
        enabled=not (path / ".disabled").exists(),
    )


def load_plugin(path: Path | str) -> PluginInfo:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise PluginError(f"plugin directory not found: {root}")
    return _manifest_info(root)


def list_plugins() -> list[PluginInfo]:
    root = plugins_dir()
    plugins: list[PluginInfo] = []
    for path in sorted(root.iterdir() if root.is_dir() else [], key=lambda item: item.name.lower()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            plugins.append(_manifest_info(path))
        except PluginError as exc:
            plugins.append(
                PluginInfo(
                    name=path.name,
                    version="?",
                    description="",
                    path=path,
                    valid=False,
                    error=str(exc),
                )
            )
    return plugins


def enabled_plugin_roots() -> list[Path]:
    return [plugin.path for plugin in list_plugins() if plugin.valid and plugin.enabled]


def plugin_skill_roots() -> list[Path]:
    return [root / "skills" for root in enabled_plugin_roots() if (root / "skills").is_dir()]


def plugin_command_roots() -> list[Path]:
    return [root / "commands" for root in enabled_plugin_roots() if (root / "commands").is_dir()]


def plugin_subagent_roots() -> list[Path]:
    return [root / "subagents" for root in enabled_plugin_roots() if (root / "subagents").is_dir()]


def plugin_hook_roots() -> list[Path]:
    return [root / "hooks" for root in enabled_plugin_roots() if (root / "hooks").is_dir()]


def plugin_mcp_servers() -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for root in enabled_plugin_roots():
        path = root / "mcp.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        items = payload.get("servers", {})
        if not isinstance(items, dict):
            continue
        for name, config in items.items():
            if isinstance(name, str) and isinstance(config, dict):
                servers.setdefault(name, config)
    return servers


def _ignored(relative: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in relative.parts) or relative.suffix in (
        _IGNORED_SUFFIXES
    )


def _scan_source(source: Path) -> None:
    total = 0
    count = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise PluginError(f"plugin contains a symlink: {path}")
        relative = path.relative_to(source)
        if _ignored(relative):
            continue
        if path.is_file():
            count += 1
            total += path.stat().st_size
    if count > MAX_PLUGIN_FILES:
        raise PluginError(f"plugin has too many files ({count} > {MAX_PLUGIN_FILES})")
    if total > MAX_PLUGIN_BYTES:
        raise PluginError(f"plugin is too large ({total} bytes > {MAX_PLUGIN_BYTES})")


def _extract_zip(source: Path) -> tuple[Path, Path]:
    temporary = Path(tempfile.mkdtemp(prefix="siwah-plugin-"))
    try:
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                target = (temporary / name).resolve()
                try:
                    target.relative_to(temporary.resolve())
                except ValueError as exc:
                    raise PluginError(
                        f"zip entry escapes the plugin directory: {info.filename}"
                    ) from exc
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PluginError(f"zip contains a symlink: {info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as handle, target.open("wb") as output:
                        shutil.copyfileobj(handle, output)
        if (temporary / PLUGIN_MANIFEST).is_file():
            return temporary, temporary
        candidates = [path for path in temporary.iterdir() if path.is_dir()]
        if len(candidates) == 1 and (candidates[0] / PLUGIN_MANIFEST).is_file():
            return candidates[0], temporary
        raise PluginError("zip does not contain plugin.toml at its root")
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def install_plugin(
    source: Path | str,
    *,
    force: bool = False,
) -> PluginInfo:
    raw = Path(source).expanduser().resolve()
    if not raw.exists():
        raise PluginError(f"plugin source not found: {raw}")
    extracted: Path | None = None
    try:
        if raw.is_file() and raw.suffix.lower() == ".zip":
            root, extracted = _extract_zip(raw)
        else:
            root = raw
        if not root.is_dir():
            raise PluginError("plugin source must be a directory or a .zip file")
        info = load_plugin(root)
        target_name = _validate_name(info.name)
        target = plugins_dir() / target_name
        if target.exists():
            if not force:
                raise PluginError(f"plugin already installed: {target_name} (use --force)")
            shutil.rmtree(target)
        _scan_source(root)
        shutil.copytree(
            root,
            target,
            ignore=shutil.ignore_patterns(*_IGNORED_NAMES, "*.pyc", "*.pyo"),
        )
        return load_plugin(target)
    finally:
        if extracted is not None:
            shutil.rmtree(extracted, ignore_errors=True)


def set_enabled(name: str, enabled: bool) -> PluginInfo:
    clean = _validate_name(name)
    path = plugins_dir() / clean
    if not (path / PLUGIN_MANIFEST).is_file():
        raise PluginError(f"plugin not installed: {clean}")
    marker = path / ".disabled"
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text("disabled\n", encoding="utf-8")
    return load_plugin(path)


def remove_plugin(name: str) -> bool:
    clean = _validate_name(name)
    path = plugins_dir() / clean
    if not path.is_dir():
        raise PluginError(f"plugin not installed: {clean}")
    shutil.rmtree(path)
    return True


__all__ = [
    "MAX_PLUGIN_BYTES",
    "MAX_PLUGIN_FILES",
    "PLUGIN_MANIFEST",
    "PluginError",
    "PluginInfo",
    "enabled_plugin_roots",
    "install_plugin",
    "list_plugins",
    "load_plugin",
    "plugin_command_roots",
    "plugin_hook_roots",
    "plugin_mcp_servers",
    "plugin_skill_roots",
    "plugin_subagent_roots",
    "plugins_dir",
    "remove_plugin",
    "set_enabled",
]
