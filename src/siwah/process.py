"""Compatibility exports for kernel-owned process cleanup."""

from .kernel.process import terminate_process_tree

__all__ = ["terminate_process_tree"]
