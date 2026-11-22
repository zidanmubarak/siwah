from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


def test_extract_section_returns_only_the_requested_version():
    text = """# Changelog

## [1.0.3] — 2026-11-22

### Fixed

- The latest fix.

## [1.0.2] — 2026-11-22

### Fixed

- The previous fix.
"""

    assert release_notes.extract_section(text, "1.0.3") == "### Fixed\n\n- The latest fix."
    assert release_notes.extract_section(text, "v1.0.2") == "### Fixed\n\n- The previous fix."


def test_extract_section_rejects_an_unknown_version():
    with pytest.raises(ValueError, match="no section"):
        release_notes.extract_section("# Changelog\n\n## [1.0.2]\n\n- old\n", "1.0.3")
