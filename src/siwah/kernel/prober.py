"""Deterministic tiered prober for synthetic evolution scenarios."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..auth import AuthStore
from ..config import Config, load_config
from .candidates import Candidate, ProbeResult
from .eval_models import EvalMetrics, EvalReport
from .evaluation import EvalRunner


@dataclass(frozen=True)
class SyntheticCheck:
    name: str
    path: str
    required: tuple[str, ...]
    weight: int = 1


class SyntheticProber:
    """Score harness files against deterministic rubric checks.

    This is the cold-start/synthetic path. It keeps the evolution engine
    testable without live model calls; the real eval suite remains available
    through `siwah eval`.
    """

    def __init__(self, checks: list[SyntheticCheck], *, t1_size: int = 2) -> None:
        self.checks = checks
        self.t1_size = max(1, t1_size)

    async def baseline(self, harness_root: Path | str, *, tier: str = "t2") -> ProbeResult:
        return self._score(Path(harness_root), tier=tier, baseline=True)

    async def probe(
        self, candidate: Candidate, harness_root: Path | str, *, tier: str = "t2"
    ) -> ProbeResult:
        root = Path(harness_root).resolve()
        with tempfile.TemporaryDirectory(prefix="siwah-probe-") as temp:
            copy = Path(temp) / "harness"
            shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            for path, content in candidate.changes.items():
                target = (copy / path).resolve()
                if not _inside(target, copy):
                    raise ValueError(f"candidate path is outside the harness: {path}")
                if content is None:
                    target.unlink(missing_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            return self._score(copy, tier=tier, baseline=False)

    def _score(self, root: Path, *, tier: str, baseline: bool) -> ProbeResult:
        selected = self.checks[: self.t1_size] if tier == "t1" else self.checks
        tasks = sum(check.weight for check in selected)
        passed = 0
        for check in selected:
            target = root / check.path
            try:
                text = target.read_text(encoding="utf-8").lower()
            except OSError:
                text = ""
            if all(required.lower() in text for required in check.required):
                passed += check.weight
        metrics = EvalMetrics(
            tasks=tasks,
            passed=passed,
            pass_rate=passed / tasks if tasks else 0.0,
            steps=tasks,
            input_tokens=0,
            output_tokens=0,
            allowance=0.0,
            duration_s=0.0,
            tool_calls=0,
            tool_errors=0,
        )
        return ProbeResult(tier=tier, golden=metrics, baseline=baseline)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ProberProtocol(Protocol):
    async def baseline(self, harness_root: Path | str, *, tier: str = "t2") -> ProbeResult: ...

    async def probe(
        self, candidate: Candidate, harness_root: Path | str, *, tier: str = "t2"
    ) -> ProbeResult: ...


AsyncEvaluate = Callable[[Path, str], Awaitable[EvalReport]]


class ProbeError(RuntimeError):
    pass


class EvalProber:
    """Live eval prober using a temporary candidate harness."""

    def __init__(
        self,
        *,
        eval_dir: Path | str | None,
        config: Config,
        auth: AuthStore,
        model: str,
        tier: str = "t2",
        max_steps: int = 16,
        max_allowance: float = 0.05,
        runs_per_task: int = 1,
        limit: int | None = None,
        evaluate: AsyncEvaluate | None = None,
    ) -> None:
        self.eval_dir = eval_dir
        self.config = config
        self.auth = auth
        self.model = model
        self.tier = tier
        self.max_steps = max_steps
        self.max_allowance = max_allowance
        self.runs_per_task = runs_per_task
        self.limit = limit
        self._evaluate = evaluate or self._run_live
        self.spent_allowance = 0.0
        self.spent_duration = 0.0

    async def baseline(self, harness_root: Path | str, *, tier: str | None = None) -> ProbeResult:
        return await self._probe(None, harness_root, tier=tier or self.tier, baseline=True)

    async def probe(
        self, candidate: Candidate, harness_root: Path | str, *, tier: str = "t2"
    ) -> ProbeResult:
        return await self._probe(candidate, harness_root, tier=tier, baseline=False)

    async def _probe(
        self,
        candidate: Candidate | None,
        harness_root: Path | str,
        *,
        tier: str,
        baseline: bool,
    ) -> ProbeResult:
        root = Path(harness_root).resolve()
        with tempfile.TemporaryDirectory(prefix="siwah-eval-probe-") as temp:
            home = Path(temp)
            copy = home / "harness"
            shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            if candidate is not None:
                for path, content in candidate.changes.items():
                    target = (copy / path).resolve()
                    if not _inside(target, copy):
                        raise ProbeError(f"candidate path is outside the harness: {path}")
                    if content is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
            report = await self._evaluate(home, tier)
        self.spent_allowance += report.metrics.allowance
        self.spent_duration += report.metrics.duration_s
        if report.stopped_early:
            raise ProbeError("live probe stopped at the allowance cap")
        if report.validation_errors:
            raise ProbeError("probe validation failed: " + "; ".join(report.validation_errors))
        return ProbeResult(tier=tier, golden=report.metrics, baseline=baseline)

    async def _run_live(self, home: Path, tier: str) -> EvalReport:
        old_home = os.environ.get("SIWAH_HOME")
        old_live = os.environ.get("SIWAH_LIVE")
        os.environ["SIWAH_HOME"] = str(home)
        os.environ["SIWAH_LIVE"] = "1"
        try:
            candidate_path = home / "harness" / "config.toml"
            candidate_config = (
                load_config(candidate_path) if candidate_path.exists() else self.config
            )
            # Routing and credentials are evaluation infrastructure, not a
            # candidate-controlled way to redirect user task data.
            candidate_config = candidate_config.model_copy(
                update={"providers": self.config.providers, "roles": self.config.roles}
            )
            runner = EvalRunner(
                eval_dir=self.eval_dir,
                cache_dir=home / "cache",
                config=candidate_config,
                auth=self.auth,
            )
            report = await runner.run(
                suite="golden",
                harness="siwah",
                tier=tier,
                model=self.model,
                runs_per_task=self.runs_per_task,
                max_steps=self.max_steps,
                limit=self.limit,
                use_cache=False,
                live=True,
                max_allowance=self.max_allowance,
            )
        finally:
            if old_home is None:
                os.environ.pop("SIWAH_HOME", None)
            else:
                os.environ["SIWAH_HOME"] = old_home
            if old_live is None:
                os.environ.pop("SIWAH_LIVE", None)
            else:
                os.environ["SIWAH_LIVE"] = old_live
        return report


__all__ = ["EvalProber", "ProbeError", "ProberProtocol", "SyntheticCheck", "SyntheticProber"]
