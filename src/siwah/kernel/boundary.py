"""Single source of truth for protected source, policy and bootstrap paths."""

PROTECTED_TREES = (
    "src/siwah/kernel",
    "src/siwah/eval",
    "src/siwah/cli",
    "evals",
    "tests/kernel",
)
PROTECTED_FILES = (
    "src/siwah/__init__.py",
    "src/siwah/__main__.py",
    "src/siwah/version.py",
    "src/siwah/config.py",
    "src/siwah/auth.py",
    "src/siwah/paths.py",
    "src/siwah/process.py",
    "src/siwah/providers/profiles.py",
    "src/siwah/providers/__init__.py",
    "pyproject.toml",
    *(
        f"src/siwah/adapt/{name}.py"
        for name in (
            "__init__",
            "canary",
            "composer",
            "engine",
            "level2",
            "models",
            "observer",
            "prober",
            "promoter",
            "proposer",
            "reflector",
            "restart",
            "state",
            "synthetic",
            "trigger",
            "worktree",
        )
    ),
)
PROTECTED_SOURCE_PATHS = (*PROTECTED_TREES, *PROTECTED_FILES)
PROTECTED_RECOVERY_PATHS = (*PROTECTED_SOURCE_PATHS, "kernel.manifest.json")
