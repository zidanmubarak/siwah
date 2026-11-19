"""Human-owned evolution policy schemas; never candidate-editable code."""

from pydantic import BaseModel, ConfigDict


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_allowance_per_cycle: float = 1.0
    max_cycles_per_day: int = 3
    reserve_weekly_pct: int = 40
    max_concurrency: int = 2
    cooldown_minutes: int = 60
    max_wall_minutes: int = 45


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    golden_regression_tolerance: float = 0.0
    heldout_noninferiority: float = 0.02
    min_gain: str = "pass_rate_up OR (steps_or_cost_down >= 0.10 with equal pass_rate)"
    max_cost_ratio: float = 1.2
    runs_per_task: int = 3


class CanaryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sessions: int = 10
    max_error_rate_increase: float = 0.15
    max_correction_rate_increase: float = 0.15


__all__ = ["BudgetConfig", "CanaryConfig", "GateConfig"]
