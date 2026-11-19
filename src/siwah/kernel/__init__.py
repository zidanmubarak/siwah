"""Protected kernel: guard, manifest, audit, snapshots, budget, supervisor."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Return the repository root for an editable/source checkout."""

    return Path(__file__).resolve().parents[3]


__all__ = ["repo_root"]
