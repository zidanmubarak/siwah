"""Platform shell selection shared by foreground, background, and local sandbox runs."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _git_bash() -> str | None:
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and "windowsapps" not in found.lower():
        return found
    if os.name == "nt":
        program_files = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        for candidate in (
            Path(program_files) / "Git" / "bin" / "bash.exe",
            Path(program_files) / "Git" / "usr" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def shell_argv(command: str) -> list[str]:
    """Return an argv that invokes a real shell for *command*.

    Windows uses Git Bash when available so the `bash` tool actually speaks
    POSIX shell. The cmd.exe fallback is only for machines without Git Bash.
    """

    bash = _git_bash()
    if bash:
        return [bash, "-lc", command]
    if os.name == "nt":
        return ["cmd", "/c", command]
    return ["/bin/sh", "-lc", command]


__all__ = ["shell_argv"]
