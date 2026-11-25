"""Allow `python -m siwah` and the supervisor launcher path."""

import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["supervise"]:
        from .cli.supervise import run_supervisor

        raise SystemExit(run_supervisor())

    from .cli.main import app

    app()
