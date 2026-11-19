"""Compatibility exports for the kernel-owned filesystem locations."""

from .kernel.paths import (
    auth_path,
    config_path,
    ensure_home,
    harness_dir,
    legacy_config_path,
    profiles_dir,
    runtime_path,
    siwah_home,
    state_path,
    trajectories_dir,
    write_private,
)

__all__ = [
    "auth_path",
    "config_path",
    "ensure_home",
    "harness_dir",
    "legacy_config_path",
    "profiles_dir",
    "runtime_path",
    "siwah_home",
    "state_path",
    "trajectories_dir",
    "write_private",
]
