from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from siwah import tui_install
from siwah import update as updater


def test_pypi_check_reports_uv_tool_updates(monkeypatch):
    monkeypatch.setattr(updater, "_latest_pypi_version", lambda: ("9.9.9", None))
    monkeypatch.setattr(updater, "_uv_tool_managed", lambda _uv=None: True)
    monkeypatch.setattr(updater, "running_from_uv_tool", lambda _uv=None: True)
    status = updater._check_remote_only()
    assert status.source == "pypi"
    assert status.latest == "9.9.9"
    assert status.available is True
    assert status.can_apply is True
    assert "uv" in (status.detail or "")


def test_pypi_failure_falls_back_to_git_tags(monkeypatch):
    monkeypatch.setattr(updater, "_latest_pypi_version", lambda: (None, "pypi offline"))
    monkeypatch.setattr(updater, "_latest_remote_tag", lambda _repo: ("9.9.9", None))
    monkeypatch.setattr(updater, "_uv_tool_managed", lambda _uv=None: False)
    status = updater._check_remote_only()
    assert status.source == "ls-remote"
    assert status.latest == "9.9.9"
    assert status.can_apply is False
    assert "pypi offline" in (status.error or "")


def test_custom_update_repo_skips_the_public_pypi_check(monkeypatch):
    monkeypatch.setenv("SIWAH_UPDATE_REPO", "example/fork")
    monkeypatch.setattr(updater, "_latest_remote_tag", lambda _repo: ("9.9.9", None))
    monkeypatch.setattr(updater, "_uv_tool_managed", lambda _uv=None: False)
    monkeypatch.setattr(
        updater,
        "_latest_pypi_version",
        lambda: pytest.fail("PyPI must not be used for a custom repository"),
    )
    status = updater._check_remote_only()
    assert status.source == "ls-remote"
    assert status.latest == "9.9.9"


def test_apply_uv_tool_upgrades_and_updates_a_staged_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    name = tui_install.tui_binary_name()
    (home / "bin").mkdir(parents=True)
    (home / "bin" / name).write_bytes(b"old")
    monkeypatch.setenv("SIWAH_HOME", str(home))
    calls: list[list[str]] = []

    def run(command, *, cwd, timeout, env=None):
        calls.append(command)
        return True, "Installed siwah 9.9.9"

    staged_calls: list[bool] = []

    def ensure(*, version=None, force=False, staged=False):
        staged_calls.append(staged)
        return home / "bin" / f"{name}.next"

    monkeypatch.setattr(updater, "source_root", lambda: None)
    monkeypatch.setattr(updater, "_find_uv", lambda _root=None: "uv")
    monkeypatch.setattr(updater, "_uv_tool_managed", lambda _uv=None: True)
    monkeypatch.setattr(updater, "running_from_uv_tool", lambda _uv=None: True)
    monkeypatch.setattr(updater, "_latest_pypi_version", lambda: ("9.9.9", None))
    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(tui_install, "ensure_tui_binary", ensure)

    result = updater.apply_update(rebuild_tui=False)
    assert result.ok is True
    assert result.to_version == "9.9.9"
    assert result.restart_required is True
    assert calls == [["uv", "tool", "upgrade", "siwah"]]
    assert staged_calls == [os.name == "nt"]
    assert any("Staged the TUI" in step for step in result.steps)


def test_update_cache_is_scoped_by_install_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    status = updater.UpdateStatus(current=updater.__version__, latest="9.9.9", source="pypi")
    updater._write_cache(status, "remote")
    assert updater._read_cache("remote") is not None
    assert updater._read_cache(f"checkout:{tmp_path}") is None


def test_apply_uv_tool_rejects_an_external_install(monkeypatch):
    monkeypatch.setattr(updater, "source_root", lambda: None)
    monkeypatch.setattr(updater, "_find_uv", lambda _root=None: "uv")
    monkeypatch.setattr(updater, "_uv_tool_managed", lambda _uv=None: False)
    result = updater.apply_update(rebuild_tui=False)
    assert result.ok is False
    assert "not running from a uv tool install" in result.detail


def test_ensure_tui_binary_can_stage_an_update(tmp_path: Path, monkeypatch):
    payload = b"tui-binary"
    asset = tui_install.release_asset_name()
    digest = hashlib.sha256(payload).hexdigest()
    client = _Client(
        _Response(text=f"{digest}  {asset}\n"),
        _Response(body=payload),
    )
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tui_install.httpx, "Client", lambda **_kwargs: client)
    path = tui_install.ensure_tui_binary(staged=True)
    assert path.name == f"{tui_install.tui_binary_name()}.next"
    assert path.read_bytes() == payload


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
