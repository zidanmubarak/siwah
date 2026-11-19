"""Package a friend-testing bundle: wheel, TUI binary, installers, checksums."""

from __future__ import annotations

import hashlib
import platform
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
INSTALL = ROOT / "scripts" / "install"


def _version() -> str:
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    version = _version()
    wheel = DIST / f"siwah-{version}-py3-none-any.whl"
    if not wheel.is_file():
        print(f"missing wheel: {wheel}", file=sys.stderr)
        return 1
    platform_name = platform.system().lower()
    arch = platform.machine().lower()
    suffix = ".exe" if sys.platform == "win32" else ""
    tui = DIST / f"siwah-tui-{platform_name}-{arch}{suffix}"
    if not tui.is_file():
        print(f"missing TUI binary: {tui}", file=sys.stderr)
        return 1

    bundle = DIST / f"siwah-{version}-{platform_name}-{arch}.zip"
    readme = (
        f"siwah {version} test bundle\n"
        "================================\n\n"
        "Requirements: uv (https://docs.astral.sh/uv/) and a terminal.\n\n"
        "Windows:\n"
        "  powershell -ExecutionPolicy Bypass -File .\\install.ps1\n\n"
        "macOS/Linux:\n"
        "  sh ./install.sh\n\n"
        "Then check and run:\n"
        "  siwah --version\n"
        "  siwah\n\n"
        "To update later, replace this bundle and run the installer again.\n"
        "Checksums are in SHA256SUMS; the TUI binary is placed in\n"
        "~/.siwah/bin/siwah-tui[.exe].\n"
    )
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in (wheel, tui, DIST / "SHA256SUMS"):
            if path.is_file():
                archive.write(path, arcname=path.name)
        for installer in (INSTALL / "install.ps1", INSTALL / "install.sh"):
            archive.write(installer, arcname=installer.name)
        archive.writestr("README.txt", readme)

    print(f"bundle: {bundle}")
    digest = _sha256(bundle)
    (DIST / f"{bundle.name}.sha256").write_text(
        f"{digest}  {bundle.name}\n", encoding="utf-8", newline="\n"
    )
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
