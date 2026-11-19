"""Trusted filesystem locations used by siwah.

`SIWAH_HOME` exists so tests and isolated runs never touch the real user home.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path


def siwah_home() -> Path:
    override = os.environ.get("SIWAH_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".siwah"


def config_path() -> Path:
    override = os.environ.get("SIWAH_CONFIG")
    if override:
        return Path(override).expanduser()
    return harness_dir() / "config.toml"


def legacy_config_path() -> Path:
    """Pre-Phase-3 config location, kept for migration only."""

    return siwah_home() / "config.toml"


def auth_path() -> Path:
    return siwah_home() / "auth.json"


def state_path() -> Path:
    return siwah_home() / "state.db"


def harness_dir() -> Path:
    return siwah_home() / "harness"


def profiles_dir() -> Path:
    return harness_dir() / "profiles"


def trajectories_dir() -> Path:
    return siwah_home() / "trajectories"


def runtime_path() -> Path:
    return siwah_home() / "runtime.json"


def ensure_home() -> Path:
    home = siwah_home()
    home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        home.chmod(stat.S_IRWXU)
    for directory in (harness_dir(), profiles_dir(), trajectories_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return home


def write_private(path: Path, content: str) -> None:
    """Write a file and restrict it to the current user where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
