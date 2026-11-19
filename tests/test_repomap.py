from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from siwah.repo.index import WorkspaceIndex


def _write_repo(root: Path) -> None:
    root.mkdir()
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    package = root / "pkg"
    package.mkdir()
    for index in range(500):
        name = f"module_{index:03d}"
        body = [f"class Service{index:03d}:", "    def run(self):", "        return 1", ""]
        body.extend(f"    VALUE_{line} = {line}" for line in range(110))
        (package / f"{name}.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    (root / "target.py").write_text(
        "class PaymentGateway:\n    def settle_invoice(self, invoice):\n        return invoice\n",
        encoding="utf-8",
    )
    ignored = root / "ignored"
    ignored.mkdir()
    (ignored / "secret.py").write_text("class HiddenThing:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=matrix",
            "-c",
            "user.email=matrix@localhost",
            "commit",
            "-qm",
            "init",
        ],
        cwd=root,
        check=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for the fixture")
def test_repo_map_fits_budget_on_60k_lines(tmp_path: Path):
    root = tmp_path / "large"
    _write_repo(root)
    lines = sum(
        len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        for path in root.glob("pkg/*.py")
    )
    assert lines >= 50_000

    index = WorkspaceIndex(root, cache_root=tmp_path / "cache")
    budget = 1500
    repo_map = index.repo_map("payment invoice settle", budget_tokens=budget)
    assert len(repo_map) <= budget * 4 + 300
    assert "PaymentGateway" in repo_map or "settle_invoice" in repo_map
    assert "HiddenThing" not in repo_map
    assert index.cache_path.exists()

    symbols = index.find_symbols("PaymentGateway", limit=5)
    assert any(symbol.name == "PaymentGateway" for _, symbol in symbols)
    refs = index.find_refs("settle_invoice", limit=5)
    assert any("target.py" in item for item in refs)
