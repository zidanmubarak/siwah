from __future__ import annotations

from pathlib import Path

import pytest

from siwah.config import PermissionsConfig
from siwah.kernel.guard import is_protected, protected_display_paths
from siwah.permissions.policy import PathGuardError, PermissionPolicy, PermissionRequest


def test_protected_paths_cover_kernel_evals_tests_and_manifest(tmp_path: Path):
    for name in (
        "src/siwah/kernel/guard.py",
        "evals/golden/golden-bugfix-01.json",
        "tests/kernel/test_guard.py",
        "kernel.manifest.json",
    ):
        assert is_protected(tmp_path / name, workspace=tmp_path)
    assert not is_protected(tmp_path / "src" / "siwah" / "core" / "loop.py", workspace=tmp_path)
    assert "evals" in protected_display_paths()
    assert "kernel.manifest.json" in protected_display_paths()


def test_permission_policy_denies_kernel_and_eval_writes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "src" / "siwah" / "kernel").mkdir(parents=True)
    (workspace / "evals" / "golden").mkdir(parents=True)
    (workspace / "tests" / "kernel").mkdir(parents=True)
    policy = PermissionPolicy(workspace, PermissionsConfig())

    for name in (
        "src/siwah/kernel/guard.py",
        "evals/golden/example.json",
        "tests/kernel/test_example.py",
        "kernel.manifest.json",
    ):
        with pytest.raises(PathGuardError):
            policy.resolve(name, write=True)

    assert (
        policy.check(
            PermissionRequest(
                tool="write_file",
                kind="write",
                preview="x",
                path="evals/golden/example.json",
            )
        )
        == "deny"
    )
    assert (
        policy.check(
            PermissionRequest(
                tool="write_file",
                kind="write",
                preview="x",
                path="src/siwah/kernel/guard.py",
            )
        )
        == "deny"
    )
