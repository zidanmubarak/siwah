"""Permission policy and path guard.

This is an application-level boundary, not a sandbox. A deliberately malicious
`bash` command can still leave the workspace; the README must say so. The guard
prevents accidental writes and protects the kernel/eval areas from the agent.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import PermissionRule, PermissionsConfig
from ..kernel import repo_root as siwah_repo_root
from ..kernel.guard import is_protected as is_protected_path
from ..paths import harness_dir

Decision = Literal["allow", "ask", "deny"]
ApprovalDecision = Literal["once", "session", "always", "deny"]


class PathGuardError(RuntimeError):
    """A path is outside the allowed roots or is explicitly protected."""


@dataclass(frozen=True)
class PermissionRequest:
    tool: str
    kind: Literal["read", "write", "bash", "network", "user"]
    preview: str
    path: str | None = None
    command: str | None = None
    patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Approval:
    decision: ApprovalDecision
    reason: str | None = None


Approver = Callable[[PermissionRequest], Awaitable[Approval]]


class PermissionPolicy:
    def __init__(
        self,
        workspace: Path,
        config: PermissionsConfig,
        *,
        home: Path | None = None,
        extra_write_roots: list[Path] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config = config
        self.home = (home or harness_dir().parent).resolve()
        self.extra_write_roots = [path.resolve() for path in (extra_write_roots or [])]
        self._session_allow: set[str] = set()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def _roots(self, write: bool) -> list[Path]:
        roots = [self.workspace, harness_dir().resolve()]
        if write:
            roots.extend(self.extra_write_roots)
        return roots

    def resolve(self, raw: str | Path, *, write: bool = False) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = self.workspace / path
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise PathGuardError(f"Cannot resolve path {raw}: {exc}") from exc
        if self.is_secret(resolved):
            raise PathGuardError(f"Access to secret-like path is denied: {raw}")
        # The workspace-relative guard is bypassed when the session cwd is itself
        # inside a protected tree; also check against this installation's root.
        root = siwah_repo_root()
        if self.is_protected(resolved) or (
            _is_relative_to(resolved, root) and is_protected_path(resolved, workspace=root)
        ):
            raise PathGuardError(f"Protected path is denied: {raw}")
        if not any(_is_relative_to(resolved, root) for root in self._roots(write)):
            raise PathGuardError(f"Path is outside the workspace: {raw}")
        return resolved

    def is_secret(self, path: Path) -> bool:
        candidates = [path.name, str(path)]
        try:
            relative = path.resolve().relative_to(self.workspace)
            candidates.append(str(relative))
        except (OSError, ValueError):
            pass
        return any(
            fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(path.name, pattern)
            for candidate in candidates
            for pattern in self.config.deny_paths
        )

    def is_protected(self, path: Path) -> bool:
        """Kernel, evals, kernel tests, and the manifest are never agent-writable."""

        return is_protected_path(path, workspace=self.workspace)

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------
    def check(self, request: PermissionRequest) -> Decision:
        if request.path:
            write = request.kind == "write"
            try:
                self.resolve(request.path, write=write)
            except PathGuardError:
                return "deny"
        if request.command and _is_catastrophic_command(request.command):
            return "deny"

        # Later rules win, so a specific deny can override an earlier broad allow.
        for rule in reversed(self.config.rules):
            if self._rule_matches(rule, request):
                return rule.decision

        key = self._key(request)
        if key in self._session_allow or f"{request.tool}:*" in self._session_allow:
            return "allow"

        if request.kind == "read":
            return "allow"
        if request.kind == "write":
            return "allow" if self.config.mode in {"auto-edit", "yolo"} else "ask"
        if request.kind == "bash":
            return "allow" if self.config.mode == "yolo" else "ask"
        if request.kind == "network":
            return "allow" if self.config.mode == "yolo" else "ask"
        return "ask"

    def approve(self, request: PermissionRequest, approval: Approval) -> None:
        if approval.decision in {"session", "always"}:
            self._session_allow.add(self._key(request))
            if approval.decision == "always":
                self._session_allow.add(f"{request.tool}:*")

    def _key(self, request: PermissionRequest) -> str:
        return f"{request.tool}:{request.path or request.command or ''}"

    def _rule_matches(self, rule: PermissionRule, request: PermissionRequest) -> bool:
        if rule.tool and not fnmatch.fnmatch(request.tool, rule.tool):
            return False
        if rule.path:
            value = request.path or ""
            if not fnmatch.fnmatch(value, rule.path):
                try:
                    relative = str(Path(value).resolve().relative_to(self.workspace))
                except (OSError, ValueError):
                    relative = value
                if not fnmatch.fnmatch(relative, rule.path):
                    return False
        if not rule.command:
            return True
        return bool(request.command and fnmatch.fnmatch(request.command, rule.command))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


_DESTRUCTIVE = [
    "rm -rf",
    "rm -r",
    "git clean -f",
    "git reset --hard",
    "git push --force",
    "git push -f",
    "del /s",
    "rd /s",
    "remove-item -recurse",
    "truncate -s 0",
]


def _command_segments(command: str) -> list[list[str]]:
    normalized = command.replace("\r", "\n")
    for separator in ("&&", "||", ";", "|", "\n"):
        normalized = normalized.replace(separator, "\n")
    segments: list[list[str]] = []
    for chunk in normalized.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            tokens = shlex.split(chunk, posix=False)
        except ValueError:
            tokens = chunk.split()
        tokens = [token.strip("'\"") for token in tokens if token]
        if tokens:
            segments.append(tokens)
    return segments


def _program_name(token: str) -> str:
    name = Path(token).name.casefold()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _is_root_target(token: str) -> bool:
    cleaned = token.strip("'\"").casefold()
    if cleaned in {"/", "/*", "~", "~/*", "c:\\", "c:/", "c:\\*", "c:/*"}:
        return True
    return re.fullmatch(r"[a-z]:[\\/]?", cleaned) is not None


def _is_catastrophic_command(command: str) -> bool:
    compact = command.replace(" ", "").casefold()
    if ":(){:|:&};:" in compact:
        return True
    for tokens in _command_segments(command):
        program = _program_name(tokens[0])
        args = [token.casefold() for token in tokens[1:]]
        if program in {"shutdown", "reboot", "halt", "poweroff"}:
            return True
        if program in {"sh", "bash", "zsh", "dash", "ksh", "cmd", "powershell", "pwsh"}:
            for position, arg in enumerate(args):
                if arg == "/c" or (arg.startswith("-") and "c" in arg[1:]):
                    inner = " ".join(args[position + 1 :])
                    if inner and _is_catastrophic_command(inner):
                        return True
        if program == "mkfs" or program.startswith("mkfs."):
            return True
        if (
            program == "dd"
            and any(arg.startswith("if=") for arg in args)
            and any(arg.startswith("of=/dev/") or arg.startswith("of=\\\\.\\") for arg in args)
        ):
            return True
        if program == "format" and any(arg.startswith("c:") for arg in args):
            return True
        recursive = any(
            arg.startswith("-") and ("r" in arg or "recurse" in arg) for arg in args
        ) or any(arg in {"/s", "-s", "-recurse"} for arg in args)
        targets = [
            arg for arg in args if not arg.startswith("-") and re.fullmatch(r"/[a-z]", arg) is None
        ]
        if recursive and any(_is_root_target(target) for target in targets):
            return True
    return False


def _is_destructive_command(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    return any(pattern in normalized for pattern in _DESTRUCTIVE)


def is_risky_command(command: str) -> bool:
    """True for commands that can destroy workspace state."""

    return _is_catastrophic_command(command) or _is_destructive_command(command)
