"""Canonical protected-path predicate for the kernel and eval trees."""

from __future__ import annotations

from pathlib import Path

from .boundary import PROTECTED_RECOVERY_PATHS

PROTECTED_PATHS = tuple(tuple(path.split("/")) for path in PROTECTED_RECOVERY_PATHS)
_UNIVERSAL_PATHS = (
    ("src", "siwah", "kernel"),
    ("evals",),
    ("tests", "kernel"),
    ("kernel.manifest.json",),
)


def _enforcement_root(root: Path) -> bool:
    """Extra dependency protection belongs to Siwah checkouts, not user projects."""
    installed = Path(__file__).resolve().parents[3]
    return root == installed or (
        (root / "kernel.manifest.json").is_file() and (root / "src/siwah/kernel/guard.py").is_file()
    )


def _matches(parts: tuple[str, ...], prefixes: tuple[tuple[str, ...], ...]) -> bool:
    lowered = tuple(part.lower() for part in parts)
    return any(lowered[: len(prefix)] == prefix for prefix in prefixes)


def protected_display_paths() -> tuple[str, ...]:
    return tuple("/".join(parts) for parts in PROTECTED_PATHS)


def is_protected(path: Path | str, *, workspace: Path | str | None = None) -> bool:
    """Return true when a path is inside a kernel/eval protected area.

    Kernel/eval path layouts remain universally reserved. Additional metadata
    and dependency paths apply only to the installed source or an evolution
    checkout carrying its kernel and manifest. An unrelated Python project's
    pyproject.toml is not Siwah's trust anchor.
    """

    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    if workspace is not None:
        root = Path(workspace).expanduser().resolve()
        try:
            parts = resolved.relative_to(root).parts
        except (OSError, ValueError):
            return False
        prefixes = PROTECTED_PATHS if _enforcement_root(root) else _UNIVERSAL_PATHS
        return _matches(parts, prefixes)
    parts = resolved.parts
    lowered = tuple(part.lower() for part in parts)
    if any(
        lowered[index : index + len(prefix)] == prefix
        for prefix in _UNIVERSAL_PATHS
        for index in range(0, len(lowered) - len(prefix) + 1)
    ):
        return True
    return any(
        _enforcement_root(root) and _matches(resolved.relative_to(root).parts, PROTECTED_PATHS)
        for root in resolved.parents
    )


__all__ = ["PROTECTED_PATHS", "is_protected", "protected_display_paths"]
