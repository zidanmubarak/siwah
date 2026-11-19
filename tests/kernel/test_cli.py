from __future__ import annotations

from typer.testing import CliRunner

from siwah.cli.main import app


def test_kernel_verify_and_manifest_commands():
    runner = CliRunner()
    verified = runner.invoke(app, ["kernel", "verify"])
    assert verified.exit_code == 0, verified.output
    assert "manifest ok" in verified.output

    shown = runner.invoke(app, ["kernel", "manifest"])
    assert shown.exit_code == 0, shown.output
    assert "kernel.manifest.json" in shown.output
