from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from siwah import tui_install


class _Response:
    def __init__(self, *, status_code: int = 200, text: str = "", body: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self._body = body

    def iter_bytes(self):
        for index in range(0, len(self._body), 3):
            yield self._body[index : index + 3]


class _Stream:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def __enter__(self) -> _Response:
        return self.response

    def __exit__(self, *_args) -> None:
        return None


class _Client:
    def __init__(self, sums: _Response, asset: _Response) -> None:
        self.sums = sums
        self.asset = asset

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, _url: str) -> _Response:
        return self.sums

    def stream(self, _method: str, _url: str) -> _Stream:
        return _Stream(self.asset)


def test_release_asset_name_and_checksum(monkeypatch):
    monkeypatch.setattr(tui_install.sys, "platform", "linux")
    monkeypatch.setattr(tui_install.platform, "machine", lambda: "x86_64")
    assert tui_install.release_asset_name() == "siwah-tui-linux-x86_64"
    digest = "a" * 64
    assert (
        tui_install._expected_checksum(
            f"{digest}  siwah-tui-linux-x86_64\n", "siwah-tui-linux-x86_64"
        )
        == digest
    )
    with pytest.raises(tui_install.TuiInstallError):
        tui_install._expected_checksum("not a checksum\n", "missing")


def test_ensure_tui_binary_verifies_and_installs(tmp_path: Path, monkeypatch):
    payload = b"tui-binary"
    asset = tui_install.release_asset_name()
    digest = hashlib.sha256(payload).hexdigest()
    client = _Client(
        _Response(text=f"{digest}  {asset}\n"),
        _Response(body=payload),
    )
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tui_install.httpx, "Client", lambda **_kwargs: client)

    path = tui_install.ensure_tui_binary()

    assert path.read_bytes() == payload
    assert path.name == tui_install.tui_binary_name()


def test_ensure_tui_binary_rejects_a_bad_checksum(tmp_path: Path, monkeypatch):
    asset = tui_install.release_asset_name()
    client = _Client(
        _Response(text=f"{'0' * 64}  {asset}\n"),
        _Response(body=b"tampered"),
    )
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tui_install.httpx, "Client", lambda **_kwargs: client)

    with pytest.raises(tui_install.TuiInstallError, match="Checksum mismatch"):
        tui_install.ensure_tui_binary()
    assert not (tmp_path / "home" / "bin" / tui_install.tui_binary_name()).exists()
