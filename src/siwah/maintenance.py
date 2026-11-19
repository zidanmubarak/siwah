"""Local artifact inspection and pruning for ``~/.siwah``."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .core.storage import Store
from .mcp.client import McpManager
from .paths import harness_dir, siwah_home

LOG_ROTATE_BYTES = 5 * 1024 * 1024
SPECIAL_TOOL_OUTPUT_DIRS = {"ses_context"}


@dataclass
class HomeReport:
    home: Path
    session_ids: set[str]
    checkpoint_dirs: list[str] = field(default_factory=list)
    checkpoint_orphans: list[str] = field(default_factory=list)
    trajectory_files: list[str] = field(default_factory=list)
    trajectory_orphans: list[str] = field(default_factory=list)
    tool_output_dirs: list[str] = field(default_factory=list)
    tool_output_orphans: list[str] = field(default_factory=list)
    log_bytes: int = 0
    tui_binary: Path | None = None
    tui_staged: Path | None = None
    harness_dirty: list[str] = field(default_factory=list)
    mcp_servers: int = 0

    @property
    def orphan_count(self) -> int:
        return (
            len(self.checkpoint_orphans)
            + len(self.trajectory_orphans)
            + len(self.tool_output_orphans)
        )


def inspect_home(store: Store) -> HomeReport:
    home = siwah_home()
    ids = store.session_ids()
    report = HomeReport(home=home, session_ids=ids)

    checkpoints = home / "checkpoints"
    if checkpoints.is_dir():
        report.checkpoint_dirs = sorted(
            path.name for path in checkpoints.iterdir() if path.is_dir()
        )
        report.checkpoint_orphans = [name for name in report.checkpoint_dirs if name not in ids]

    trajectories = home / "trajectories"
    if trajectories.is_dir():
        report.trajectory_files = sorted(path.stem for path in trajectories.glob("*.jsonl"))
        report.trajectory_orphans = [name for name in report.trajectory_files if name not in ids]

    outputs = home / "tool_outputs"
    if outputs.is_dir():
        report.tool_output_dirs = sorted(path.name for path in outputs.iterdir() if path.is_dir())
        report.tool_output_orphans = [
            name
            for name in report.tool_output_dirs
            if name.startswith("ses_") and name not in ids and name not in SPECIAL_TOOL_OUTPUT_DIRS
        ]

    log = home / "logs" / "daemon.log"
    if log.is_file():
        report.log_bytes = log.stat().st_size

    binary = home / "bin" / "siwah-tui.exe"
    staged = home / "bin" / "siwah-tui.exe.next"
    report.tui_binary = binary if binary.is_file() else None
    report.tui_staged = staged if staged.is_file() else None

    harness = harness_dir()
    if (harness / ".git").exists():
        try:
            completed = subprocess.run(
                ["git", "-C", str(harness), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                report.harness_dirty = [
                    line for line in completed.stdout.splitlines() if line.strip()
                ]
        except (OSError, subprocess.SubprocessError):
            pass

    report.mcp_servers = len(McpManager.load().describe())
    return report


def prune_orphans(store: Store, *, dry_run: bool = False) -> list[Path]:
    """Remove checkpoints/trajectories/tool outputs whose session is gone."""

    home = siwah_home()
    ids = store.session_ids()
    removed: list[Path] = []

    checkpoints = home / "checkpoints"
    if checkpoints.is_dir():
        for path in checkpoints.iterdir():
            if path.is_dir() and path.name not in ids:
                removed.append(path)
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)

    trajectories = home / "trajectories"
    if trajectories.is_dir():
        for path in trajectories.glob("*.jsonl"):
            if path.stem not in ids:
                removed.append(path)
                if not dry_run:
                    path.unlink(missing_ok=True)

    outputs = home / "tool_outputs"
    if outputs.is_dir():
        for path in outputs.iterdir():
            if (
                path.is_dir()
                and path.name.startswith("ses_")
                and path.name not in ids
                and path.name not in SPECIAL_TOOL_OUTPUT_DIRS
            ):
                removed.append(path)
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)

    return removed


def rotate_daemon_log(*, dry_run: bool = False) -> Path | None:
    """Rotate a large daemon log. Returns the rotated path when it happened."""

    log = siwah_home() / "logs" / "daemon.log"
    if not log.is_file() or log.stat().st_size < LOG_ROTATE_BYTES:
        return None
    target = log.with_suffix(".log.1")
    if dry_run:
        return target
    try:
        if target.exists():
            target.unlink()
        log.replace(target)
    except OSError:
        return None
    return target


__all__ = [
    "HomeReport",
    "LOG_ROTATE_BYTES",
    "inspect_home",
    "prune_orphans",
    "rotate_daemon_log",
]
