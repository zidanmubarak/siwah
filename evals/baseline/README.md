# Naive baseline

The naive harness is a fixed comparison point, not a second product:

- `AgentLoop` with the MVP tools only: `read_file`, `write_file`, `edit_file`,
  `glob`, `grep`, `bash`, `todo`.
- No verifier, repo map, LSP refs, context compaction, checkpoints, plan mode,
  memory, skills, or recall tools.
- The same deterministic mock model and task scripts as the siwah harness.

Runtime results are cached under `~/.siwah/eval/cache/` and keyed by task JSON,
harness, model, seed, and run index. The committed suite does not include
machine-specific baseline scores; `siwah eval --baseline` computes or reuses the
cache.
