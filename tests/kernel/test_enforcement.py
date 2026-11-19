"""Immutable authority, IPC and stale-evidence regression tests (offline)."""

from __future__ import annotations

import ast
import asyncio
import difflib
from pathlib import Path

import pytest

from siwah.adapt.level2 import CoreRepository, Level2Evolver
from siwah.adapt.promoter import HarnessRepository, Promoter, PromotionError
from siwah.config import PermissionsConfig
from siwah.eval.models import BudgetSuggestion, EvalMetrics, EvalReport, EvalTask
from siwah.eval.runner import EvalRunner
from siwah.kernel import eval_cli
from siwah.kernel.approval import _issue_approval
from siwah.kernel.boundary import PROTECTED_RECOVERY_PATHS, PROTECTED_SOURCE_PATHS
from siwah.kernel.candidates import Candidate
from siwah.kernel.gate import check_level2_diff
from siwah.kernel.guard import is_protected
from siwah.kernel.level2 import default_level2_commands
from siwah.kernel.manifest import file_hashes, verify_manifest, write_manifest
from siwah.kernel.policy import GateConfig
from siwah.kernel.snapshot import PROTECTED_ROLLBACK_PATHS
from siwah.permissions.policy import PathGuardError, PermissionPolicy

ROOT = Path(__file__).resolve().parents[2]


def _diff(path: str, before: str = "old\n", after: str = "new\n") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _repo(tmp_path: Path) -> HarnessRepository:
    root = tmp_path / "repo"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts/system.md").write_text("old\n", encoding="utf-8")
    repo = HarnessRepository(root)
    repo.ensure()
    return repo


def _candidate() -> Candidate:
    return Candidate(
        id="fixture",
        level=1,
        kind="prompt",
        scope="global",
        target_paths=["prompts/system.md"],
        changes={"prompts/system.md": "new\n"},
        rationale="fixture",
        expected_metric="fixture",
        risk="fixture",
    )


def _eval_fixture(tmp_path: Path, worker: str, monkeypatch) -> EvalRunner:
    subject = tmp_path / "subject"
    (subject / "siwah/kernel").mkdir(parents=True)
    for name in ("siwah/__init__.py", "siwah/kernel/__init__.py"):
        (subject / name).write_text("", encoding="utf-8")
    (subject / "siwah/kernel/subject_worker.py").write_text(worker, encoding="utf-8")
    monkeypatch.setenv("SIWAH_SUBJECT_SOURCE", str(subject))
    task = EvalTask(
        id="fixture-task",
        category="comprehension",
        language="python",
        difficulty="easy",
        prompt="Read the fixture.",
        files={"check.py": "print('original check')\n"},
        protected=["check.py"],
        check="{python} check.py",
        check_text="expected",
    )
    golden = tmp_path / "evals/golden"
    golden.mkdir(parents=True)
    (golden / "fixture.json").write_text(task.model_dump_json(), encoding="utf-8")
    return EvalRunner(eval_dir=golden.parent, cache_dir=tmp_path / "cache")


def test_compatibility_exports_have_one_protected_owner():
    assert EvalRunner.__module__ == "siwah.kernel.evaluation"
    assert Level2Evolver.__module__ == "siwah.kernel.level2"
    assert Promoter.__module__ == "siwah.kernel.promotion"
    assert Candidate.__module__ == "siwah.kernel.candidates"


def test_guard_admission_manifest_and_recovery_cover_the_same_boundary():
    hashes = file_hashes(ROOT)
    assert PROTECTED_ROLLBACK_PATHS == PROTECTED_RECOVERY_PATHS
    for path in PROTECTED_SOURCE_PATHS:
        candidate = path if Path(path).suffix else f"{path}/fixture.py"
        assert is_protected(ROOT / candidate, workspace=ROOT), path
        _stats, errors = check_level2_diff(_diff(candidate))
        assert any("forbidden" in error for error in errors), path
        target = ROOT / path
        assert any(name == path or name.startswith(path + "/") for name in hashes), path
        assert target.exists(), path


def test_user_project_metadata_is_not_installation_metadata(tmp_path: Path):
    project = tmp_path / "user-project"
    project.mkdir()
    metadata = project / "pyproject.toml"
    metadata.write_text("[project]\nname = 'user-project'\n", encoding="utf-8")
    policy = PermissionPolicy(project, PermissionsConfig(mode="yolo"))
    assert policy.resolve("pyproject.toml", write=False) == metadata
    assert policy.resolve("pyproject.toml", write=True) == metadata
    assert not is_protected(metadata)

    evolution = tmp_path / "evolution"
    (evolution / "src/siwah/kernel").mkdir(parents=True)
    (evolution / "src/siwah/kernel/guard.py").write_text("", encoding="utf-8")
    (evolution / "kernel.manifest.json").write_text("{}", encoding="utf-8")
    (evolution / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    guarded = PermissionPolicy(evolution, PermissionsConfig(mode="yolo"))
    with pytest.raises(PathGuardError):
        guarded.resolve("pyproject.toml", write=True)
    assert is_protected(ROOT / "pyproject.toml", workspace=ROOT)


def test_trusted_package_ancestor_initializers_are_hashed():
    hashes = file_hashes(ROOT)
    for relative in hashes:
        if not relative.startswith("src/siwah/") or not relative.endswith(".py"):
            continue
        parent = (ROOT / relative).parent
        while parent.is_relative_to(ROOT / "src/siwah"):
            initializer = parent / "__init__.py"
            if initializer.is_file():
                name = initializer.relative_to(ROOT).as_posix()
                assert name in hashes, (relative, name)
                assert is_protected(initializer, workspace=ROOT)
                assert any("forbidden" in error for error in check_level2_diff(_diff(name))[1])
            parent = parent.parent


def test_kernel_import_time_dependencies_are_protected():
    def imports(nodes):
        for node in nodes:
            if isinstance(node, ast.If):
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    continue
                yield from imports([*node.body, *node.orelse])
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                yield node

    for path in (ROOT / "src/siwah/kernel").glob("*.py"):
        if path.name == "subject_worker.py":
            continue  # The only interpreter allowed to import mutable SUT code.
        for node in imports(ast.parse(path.read_text(encoding="utf-8")).body):
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module:
                module = ROOT / "src/siwah" / node.module.replace(".", "/")
                target = module.with_suffix(".py") if module.with_suffix(".py").exists() else module
                assert is_protected(target, workspace=ROOT), (path, node.module)


@pytest.mark.parametrize(
    "relative",
    [
        "src/siwah/config.py",
        "src/siwah/__init__.py",
        "src/siwah/paths.py",
        "src/siwah/adapt/engine.py",
        "src/siwah/eval/runner.py",
        "src/siwah/providers/__init__.py",
    ],
)
def test_manifest_detects_changed_or_missing_shared_authority(tmp_path: Path, relative: str):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    write_manifest(tmp_path)
    target.write_text("changed\n", encoding="utf-8")
    assert relative in verify_manifest(tmp_path).changed
    target.unlink()
    assert relative in verify_manifest(tmp_path).missing


def test_direct_promoters_require_kernel_gate_evidence(tmp_path: Path):
    repo = _repo(tmp_path)
    head = repo.head()
    with pytest.raises(PromotionError, match="gate approval"):
        Promoter(repo).promote(_candidate())
    with pytest.raises(RuntimeError, match="gate approval"):
        CoreRepository(repo.root).promote(_diff("prompts/system.md"), "fixture")
    assert repo.head() == head
    assert repo.clean()


def test_receipt_is_bound_to_candidate_and_consumed_on_failure(tmp_path: Path):
    repo = _repo(tmp_path)
    candidate = _candidate()
    approval = _issue_approval(
        repo.root, repo.head(), candidate.model_dump_json(), policy="fixture"
    )
    changed = candidate.model_copy(update={"changes": {"prompts/system.md": "different\n"}})
    with pytest.raises(PromotionError, match="does not match"):
        Promoter(repo).promote(changed, approval=approval)
    with pytest.raises(PromotionError, match="fresh"):
        Promoter(repo).promote(candidate, approval=approval)
    assert repo.clean()


def test_receipt_rejects_head_change_without_losing_new_work(tmp_path: Path):
    repo = _repo(tmp_path)
    candidate = _candidate()
    approval = _issue_approval(
        repo.root, repo.head(), candidate.model_dump_json(), policy="fixture"
    )
    (repo.root / "user.txt").write_text("keep", encoding="utf-8")
    repo.commit("user change")
    with pytest.raises(PromotionError, match="does not match"):
        Promoter(repo).promote(candidate, approval=approval)
    assert (repo.root / "user.txt").read_text(encoding="utf-8") == "keep"


def test_production_commands_cannot_be_replaced_by_an_empty_gate(tmp_path: Path):
    with pytest.raises(ValueError, match="test fixtures"):
        Level2Evolver(tmp_path, commands=[])
    with pytest.raises(ValueError, match="empty"):
        Level2Evolver(tmp_path, commands=[], testing=True)
    commands = default_level2_commands()
    assert {"pytest", "ruff", "pyright", "smoke", "eval-t0", "eval-t1", "eval-t2", "eval-t3"} <= {
        command.name for command in commands
    }
    assert all("-I" in command.command for command in commands if command.name.startswith("eval-"))
    assert all(
        "siwah.kernel.eval_cli" in command.command[4]
        for command in commands
        if command.name.startswith("eval-")
    )


def test_subject_cannot_supply_a_passed_verdict(tmp_path: Path, monkeypatch):
    runner = _eval_fixture(
        tmp_path,
        ("import json\ndef main(): print(json.dumps({'text': 'expected', 'passed': True}))\n"),
        monkeypatch,
    )
    report = asyncio.run(runner.run(use_cache=False))
    assert report.metrics.passed == 0
    assert "invalid observations" in (report.tasks[0].error or "")


def test_terminal_error_cannot_be_counted_as_success(tmp_path: Path, monkeypatch):
    runner = _eval_fixture(
        tmp_path,
        ("import json\ndef main(): print(json.dumps({'text': 'expected', 'error': 'aborted'}))\n"),
        monkeypatch,
    )
    report = asyncio.run(runner.run(use_cache=False))
    assert report.tasks[0].check_text_ok
    assert report.metrics.passed == 0


def test_changed_check_is_rejected_before_execution(tmp_path: Path, monkeypatch):
    runner = _eval_fixture(
        tmp_path,
        (
            "import json, sys\nfrom pathlib import Path\n"
            "def main():\n"
            " p = json.load(sys.stdin)\n"
            " Path(p['workspace'], 'check.py').write_text(\"raise RuntimeError('must not execute')\")\n"
            " print(json.dumps({'text': 'expected'}))\n"
        ),
        monkeypatch,
    )
    report = asyncio.run(runner.run(use_cache=False))
    assert report.tasks[0].guard_violation
    assert report.tasks[0].check_output == "protected evaluation input changed"
    assert report.metrics.passed == 0


def test_source_changes_invalidate_cached_results(tmp_path: Path, monkeypatch):
    runner = _eval_fixture(
        tmp_path,
        ("import json\ndef main(): print(json.dumps({'text': 'expected'}))\n"),
        monkeypatch,
    )
    first = asyncio.run(runner.run())
    assert first.metrics.passed == 1
    worker = tmp_path / "subject/siwah/kernel/subject_worker.py"
    worker.write_text(
        "import json\ndef main(): print(json.dumps({'text': 'wrong'}))\n", encoding="utf-8"
    )
    second = asyncio.run(runner.run())
    assert second.cache_hits == 0
    assert second.metrics.passed == 0


def test_t3_heldout_regression_blocks_golden_gain(monkeypatch):
    monkeypatch.setenv("SIWAH_SUBJECT_SOURCE", "candidate")
    calls = []

    async def fixture(self, *, suite, **kwargs):
        import os

        baseline = os.environ.get("SIWAH_SUBJECT_SOURCE") == "baseline"
        calls.append((suite, baseline, kwargs["use_cache"]))
        passed = 8 if baseline else (9 if suite == "golden" else 6)
        return EvalReport(
            suite=suite,
            harness="siwah",
            model="mock/model-a",
            tier="t3",
            seed=0,
            runs_per_task=3,
            started_at="fixture",
            duration_s=0,
            metrics=EvalMetrics(tasks=10, passed=passed, pass_rate=passed / 10),
            budget_suggestion=BudgetSuggestion(max_allowance_per_cycle=0),
        )

    monkeypatch.setattr(eval_cli.EvalRunner, "run", fixture)
    ok, summary = asyncio.run(eval_cli._evaluate("t3", "baseline", GateConfig()))
    assert not ok
    assert "heldout pass rate is inferior" in summary["gate"]["reasons"]
    assert calls == [
        ("golden", True, False),
        ("golden", False, False),
        ("heldout", True, False),
        ("heldout", False, False),
    ]
