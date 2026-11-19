"""Compatibility exports for protected probe execution."""

from ..kernel.prober import (
    EvalProber,
    ProbeError,
    ProberProtocol,
    SyntheticCheck,
    SyntheticProber,
)

__all__ = ["EvalProber", "ProbeError", "ProberProtocol", "SyntheticCheck", "SyntheticProber"]
