"""Build release artifacts: Python wheel, TUI binary, and SHA-256 sums.

This command never publishes. It prints the TestPyPI command for an explicit
human decision after checksums have been reviewed.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
TUI = ROOT / "ui" / "tui"


def main() -> int:
    DIST.mkdir(exist_ok=True)
    _run(["uv", "build", "--wheel", "--out-dir", str(DIST)], cwd=ROOT)
    bun = _find_bun()
    if bun is None:
        print("warning: Bun not found; TUI binary was not built", file=sys.stderr)
    else:
        binary_name = "siwah-tui.exe" if sys.platform == "win32" else "siwah-tui"
        build_stem = "siwah-tui-build"
        build_name = f"{build_stem}.exe" if sys.platform == "win32" else build_stem
        env = os.environ.copy()
        env["SIWAH_TUI_OUTFILE"] = build_stem
        _run([str(bun), "run", "build"], cwd=TUI, env=env)
        source = TUI / "dist" / build_name
        if source.is_file():
            target = DIST / f"siwah-tui-{platform.system().lower()}-{platform.machine().lower()}"
            if sys.platform == "win32":
                target = target.with_suffix(".exe")
            shutil.copy2(source, target)
            try:
                shutil.copy2(source, TUI / "dist" / binary_name)
            except OSError:
                print(
                    "warning: could not replace the development TUI binary; "
                    "close the running TUI and rerun if you need it refreshed",
                    file=sys.stderr,
                )
            source.unlink(missing_ok=True)
        else:
            print(f"warning: TUI build output not found: {source}", file=sys.stderr)

    artifacts = sorted(
        path
        for path in DIST.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.name != "SHA256SUMS"
        and path.suffix not in {".zip"}
        and not path.name.endswith(".sha256")
    )
    lines = [f"{_sha256(path)}  {path.name}" for path in artifacts]
    (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("\n".join(lines))
    print("\nReview the artifacts, then publish explicitly with:")
    print("  uvx twine upload --repository testpypi dist/*.whl")
    print("  # binary artifacts are attached to the release, not uploaded to TestPyPI")
    return 0


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=env)
    if completed.returncode != 0:
        raise SystemExit(f"command failed ({completed.returncode}): {' '.join(command)}")


def _find_bun() -> Path | None:
    found = shutil.which("bun")
    if found:
        return Path(found)
    for candidate in (ROOT / ".tools" / "bun" / "bun.exe", ROOT / ".tools" / "bun" / "bun"):
        if candidate.is_file():
            return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
