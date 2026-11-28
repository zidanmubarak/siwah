"""Plan and execute a local siwah uninstall.

The package command is not removed while this process is running. The command
removes the TUI and optional user data, then prints the package-manager command
for the user to run after exit. A standalone script covers full removal from
outside siwah.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .kernel.supervisor import read_runtime
from .paths import siwah_home
from .tui_install import tui_binary_name
from .update import running_from_uv_tool, source_root


class UninstallError(RuntimeError):
    """The uninstall plan is unsafe or cannot be executed."""


@dataclass(frozen=True)
class ToolRemoval:
    label: str
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class UninstallPlan:
    home: Path
    purge: bool
    keep_source: bool
    remove_now: tuple[Path, ...] = ()
    data_paths: tuple[Path, ...] = ()
    tool: ToolRemoval = field(default_factory=lambda: ToolRemoval(""))
    source: Path | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    removed: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _safe_root(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if len(resolved.parts) < 2:
        return False
    if resolved == Path(resolved.anchor):
        return False
    return resolved != Path.home().resolve()


def _children(path: Path, *, keep_source: bool) -> tuple[Path, ...]:
    if not path.is_dir():
        return ()
    values: list[Path] = []
    for child in sorted(path.iterdir()):
        if keep_source and child.name == "src":
            continue
        values.append(child)
    return tuple(values)


def _find_uv() -> str | None:
    for candidate in (
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
        str(Path.home() / ".cargo" / "bin" / ("uv.exe" if os.name == "nt" else "uv")),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _uv_tool_names(uv: str) -> set[str]:
    try:
        result = subprocess.run(
            [uv, "tool", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts:
            names.add(parts[0].lower())
    return names


def _running_from_pipx() -> bool:
    parts = {part.lower() for part in Path(sys.prefix).resolve().parts}
    return "pipx" in parts and "venvs" in parts and Path(sys.prefix).name.lower() == "siwah"


def _pipx_has_siwah(pipx: str) -> bool:
    try:
        result = subprocess.run(
            [pipx, "list", "--short"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[:1] == ["siwah"] for line in result.stdout.splitlines())


def detect_tool_removal() -> ToolRemoval:
    uv = _find_uv()
    if uv is not None and "siwah" in _uv_tool_names(uv) and running_from_uv_tool(uv):
        return ToolRemoval(label="uv", command=(uv, "tool", "uninstall", "siwah"))
    pipx = shutil.which("pipx")
    if pipx is not None and _pipx_has_siwah(pipx) and _running_from_pipx():
        return ToolRemoval(label="pipx", command=(pipx, "uninstall", "siwah"))
    return ToolRemoval(label="")


def build_uninstall_plan(
    *, purge: bool, keep_source: bool = False, keep_tui: bool = False
) -> UninstallPlan:
    home = siwah_home().expanduser().resolve()
    notes: list[str] = []
    remove_now: list[Path] = []
    if not keep_tui:
        for name in (tui_binary_name(), f"{tui_binary_name()}.next"):
            path = home / "bin" / name
            if path.exists():
                remove_now.append(path)
    for name in ("runtime.json", "update-check.json"):
        path = home / name
        if path.exists():
            remove_now.append(path)

    data_paths: tuple[Path, ...] = ()
    if purge:
        if not _safe_root(home):
            raise UninstallError(f"Refusing to purge an unsafe path: {home}")
        data_paths = _children(home, keep_source=keep_source)

    tool = detect_tool_removal()
    if tool.command is None:
        notes.append(
            "The siwah package is not managed by uv or pipx. Remove it with the installer you used."
        )
    if tool.command is not None:
        notes.append(
            "The siwah command is not removed while it is running; run the printed "
            "package-manager command after this process exits."
        )

    checkout = source_root()
    if checkout is not None:
        if not checkout.is_relative_to(home):
            notes.append(f"Source checkout outside {home} is not removed: {checkout}")
        elif purge and not keep_source:
            notes.append(f"Source checkout will be removed: {checkout}")
        elif keep_source:
            notes.append(f"Source checkout will be kept: {checkout}")

    return UninstallPlan(
        home=home,
        purge=purge,
        keep_source=keep_source,
        remove_now=tuple(dict.fromkeys(remove_now)),
        data_paths=data_paths,
        tool=tool,
        source=checkout,
        notes=tuple(notes),
    )


def plan_lines(plan: UninstallPlan) -> list[str]:
    lines = [
        f"home:   {plan.home}",
        f"data:   {'remove' if plan.purge else 'keep'}",
    ]
    if plan.remove_now:
        lines.append("remove now:")
        lines.extend(f"  {path}" for path in plan.remove_now)
    if plan.data_paths:
        lines.append("remove:")
        lines.extend(f"  {path}" for path in plan.data_paths)
    if plan.tool.command is not None:
        command = " ".join(plan.tool.command)
        lines.append(f"tool:   {command} (run after this process exits)")
    else:
        lines.append("tool:   not managed by uv or pipx")
    lines.extend(f"note:   {note}" for note in plan.notes)
    return lines


def _remove_path(path: Path, *, home: Path | None = None) -> str | None:
    if home is not None:
        try:
            if path.resolve() == home.resolve() or not path.resolve().is_relative_to(home):
                return f"refusing to remove path outside {home}: {path}"
        except OSError as exc:
            return f"cannot resolve {path}: {exc}"
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot remove {path}: {exc}"
    return None


def _stop_daemon() -> list[str]:
    messages: list[str] = []
    data = read_runtime()
    if isinstance(data, dict):
        pid = data.get("pid")
        if isinstance(pid, int) and pid != os.getpid():
            if os.name == "nt":
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    messages.append(f"stopped daemon pid {pid}")
            else:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
                    messages.append(f"stopped daemon pid {pid}")
    runtime = siwah_home() / "runtime.json"
    with contextlib.suppress(OSError):
        runtime.unlink(missing_ok=True)
    return messages


def execute_plan(plan: UninstallPlan) -> UninstallResult:
    removed: list[Path] = []
    errors: list[str] = []
    notes: list[str] = list(plan.notes)

    try:
        cwd = Path.cwd().resolve()
        if cwd == plan.home or cwd.is_relative_to(plan.home):
            os.chdir(Path.home())
    except OSError:
        pass
    notes.extend(_stop_daemon())
    for path in plan.remove_now:
        error = _remove_path(path, home=plan.home)
        if error is None:
            removed.append(path)
        else:
            errors.append(error)

    if plan.purge:
        for path in plan.data_paths:
            error = _remove_path(path, home=plan.home)
            if error is None:
                removed.append(path)
            else:
                errors.append(error)
        if not plan.keep_source:
            with contextlib.suppress(OSError):
                plan.home.rmdir()

    if plan.tool.command is not None:
        notes.append(
            "The siwah command is still installed; after this process exits, run: "
            + " ".join(plan.tool.command)
        )
    elif plan.purge:
        notes.append("Remove the siwah package with the installer you used.")

    return UninstallResult(
        removed=tuple(removed),
        errors=tuple(errors),
        notes=tuple(notes),
    )


__all__ = [
    "ToolRemoval",
    "UninstallError",
    "UninstallPlan",
    "UninstallResult",
    "build_uninstall_plan",
    "detect_tool_removal",
    "execute_plan",
    "plan_lines",
]
