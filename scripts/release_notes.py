"""Extract one release's section from CHANGELOG.md.

GitHub release notes should describe only the version being published, not the
entire project history. The repository changelog keeps the full history; this
script isolates the matching `## [x.y.z]` section for `gh release create
--notes-file`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extract_section(text: str, version: str) -> str:
    normalized = version.strip().removeprefix("v")
    if not normalized:
        raise ValueError("version is empty")
    pattern = re.compile(
        rf"^## \[{re.escape(normalized)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {normalized}")
    body = match.group(1).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {normalized} is empty")
    return body


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: release_notes.py <version>", file=sys.stderr)
        return 2
    try:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        print(extract_section(text, args[0]))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
