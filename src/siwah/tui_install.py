"""Download the platform TUI binary attached to a siwah release."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import uuid
from pathlib import Path

import httpx

from .paths import ensure_home, siwah_home
from .update import DEFAULT_REPO
from .version import __version__

MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024


class TuiInstallError(RuntimeError):
    """The release binary could not be resolved, downloaded, or verified."""


def tui_binary_name() -> str:
    return "siwah-tui.exe" if os.name == "nt" else "siwah-tui"


def release_asset_name() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if machine not in {"amd64", "x86_64"}:
            raise TuiInstallError(f"Unsupported Windows architecture: {machine}")
        return "siwah-tui-windows-amd64.exe"
    if sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "siwah-tui-darwin-arm64"
        if machine in {"x86_64", "amd64"}:
            return "siwah-tui-darwin-x64"
        raise TuiInstallError(f"Unsupported macOS architecture: {machine}")
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "siwah-tui-linux-x86_64"
        if machine in {"aarch64", "arm64"}:
            return "siwah-tui-linux-aarch64"
        raise TuiInstallError(f"Unsupported Linux architecture: {machine}")
    raise TuiInstallError(f"Unsupported platform: {sys.platform}")


def release_base_url(version: str | None = None) -> str:
    tag = f"v{(version or __version__).lstrip('v')}"
    return f"https://github.com/{DEFAULT_REPO}/releases/download/{tag}"


def _expected_checksum(sums: str, asset: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            digest = parts[0].lower()
            if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                return digest
    raise TuiInstallError(f"SHA256SUMS does not contain {asset}")


def ensure_tui_binary(
    *, version: str | None = None, force: bool = False, staged: bool = False
) -> Path:
    """Install the matching TUI binary under ``~/.siwah/bin`` and return it.

    ``staged`` writes ``<name>.next`` instead of replacing the active binary.
    The launcher promotes it on the next start; this is required on Windows
    while the current TUI is still running.
    """

    ensure_home()
    name = tui_binary_name()
    target = siwah_home().resolve() / "bin" / (f"{name}.next" if staged else name)
    if target.is_file() and not force:
        return target
    asset = release_asset_name()
    base = release_base_url(version)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.download")
    digest = hashlib.sha256()
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0)) as client:
            sums_response = client.get(f"{base}/SHA256SUMS")
            if sums_response.status_code != 200:
                raise TuiInstallError(
                    f"No SHA256SUMS for siwah {(version or __version__)} "
                    f"(HTTP {sums_response.status_code})."
                )
            expected = _expected_checksum(sums_response.text, asset)
            with client.stream("GET", f"{base}/{asset}") as response:
                if response.status_code != 200:
                    raise TuiInstallError(
                        f"No {asset} for siwah {(version or __version__)} "
                        f"(HTTP {response.status_code})."
                    )
                total = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise TuiInstallError("TUI download exceeded the size limit.")
                        digest.update(chunk)
                        output.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise TuiInstallError(
                f"Checksum mismatch for {asset}: expected {expected}, got {actual}."
            )
        if os.name != "nt":
            temporary.chmod(0o755)
        temporary.replace(target)
    except httpx.HTTPError as exc:
        raise TuiInstallError(f"Could not download the TUI binary: {exc}") from exc
    except OSError as exc:
        raise TuiInstallError(f"Could not activate the TUI binary: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


__all__ = [
    "TuiInstallError",
    "ensure_tui_binary",
    "release_asset_name",
    "release_base_url",
    "tui_binary_name",
]
