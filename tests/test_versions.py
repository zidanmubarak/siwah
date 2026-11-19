"""Keep distribution, runtime and generated metadata aligned across releases."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from siwah.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_and_runtime_versions_are_synchronized():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "ui/tui/package.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "ui/tui/openapi.json").read_text(encoding="utf-8"))
    locked = next(item for item in lock["package"] if item["name"] == "siwah")
    for name, version in {
        "Python distribution": project["project"]["version"],
        "Python lockfile": locked["version"],
        "TUI package": package["version"],
        "OpenAPI metadata": schema["info"]["version"],
    }.items():
        assert version == __version__, f"{name} version is out of sync"
    entry = (ROOT / "ui/tui/src/index.tsx").read_text(encoding="utf-8")
    assert f'console.log("siwah-tui {__version__}")' in entry
