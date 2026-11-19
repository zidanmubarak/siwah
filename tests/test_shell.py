from __future__ import annotations

import os
import shutil
from pathlib import Path

from siwah.shell import shell_argv


def test_shell_argv_uses_a_real_shell():
    argv = shell_argv("echo siwah")
    assert argv[-1] == "echo siwah"
    if os.name == "nt":
        assert "bash" in argv[0].lower() or "cmd" in argv[0].lower()
        if shutil.which("bash"):
            assert "system32" not in argv[0].lower()
    else:
        assert argv[1] == "-lc"
        assert Path(argv[0]).name in {"sh", "bash", "dash", "zsh"}
        assert Path(argv[0]).is_file()
