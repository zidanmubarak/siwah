"""Start the supervisor without importing the interactive CLI."""

from __future__ import annotations

import contextlib
import signal

from ..kernel import repo_root
from ..kernel.supervisor import Supervisor, boot_daemon_once, stop_daemon


def run_supervisor() -> int:
    supervisor = Supervisor(repo_root(), boot=boot_daemon_once)

    def stop(_signum: int, _frame: object) -> None:
        stop_daemon()
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(sig, stop)
    return supervisor.run()
