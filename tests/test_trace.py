from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from siwah.cli.main import app
from siwah.core.storage import Store
from siwah.core.types import Usage


def test_trace_cli_reads_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
    store = Store()
    try:
        session = store.create_session(cwd=str(tmp_path), model="mock/model-a")
        store.record_trace(
            session_id=session.id,
            step_id="step_1",
            provider="mock",
            model="model-a",
            latency_ms=12.5,
            usage=Usage(input_tokens=10, output_tokens=2, allowance=0.001),
            tool_names=["read_file"],
        )
    finally:
        store.close()

    result = CliRunner().invoke(app, ["trace", session.id])
    assert result.exit_code == 0, result.output
    assert "step_1" in result.output
    assert "read_file" in result.output

    as_json = CliRunner().invoke(app, ["trace", session.id, "--json"])
    assert as_json.exit_code == 0
    assert '"latency_ms": 12.5' in as_json.output
