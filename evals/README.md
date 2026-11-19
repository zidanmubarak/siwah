# Evaluation fixtures

This directory contains the public evaluation fixtures used by `siwah eval` and
by the evolution gate. The running agent's path guard must never write here. The
runner copies task files into a separate temporary workspace and does not expose
the task JSON to the agent.

## Layout

```text
evals/
├─ golden/      30 public, independently checkable tasks
├─ heldout/     3 public samples; the full suite is distributed separately
├─ traps/       3 public guard and injection traps
└─ baseline/    notes for the fixed naive baseline
```

A task is one JSON document. The schema is described in
[Evaluation](../docs/EVALUATION.md#task-format). In brief, a task defines an ID,
category, language, difficulty, prompt, initial files, protected files, a check,
and an optional mock script or trap marker.

## Running

From a source checkout:

```bash
uv run siwah eval --suite golden
uv run siwah eval --suite golden --baseline
uv run siwah eval --suite golden --tier t0
uv run siwah eval --suite traps
uv run siwah eval --suite heldout
```

If the fixtures are not next to the source, point the runner at them:

```bash
uv run siwah eval --suite golden --eval-dir /path/to/evals
SIWAH_EVAL_DIR=/path/to/evals uv run siwah eval --suite golden
```

The full heldout suite is not in this repository. When you have it, set
`SIWAH_HELDOUT_DIR` to its directory:

```bash
SIWAH_HELDOUT_DIR=/path/to/heldout uv run siwah eval --suite heldout
```

## Public versus external heldout

- **Golden** tasks are public so contributors can reproduce failures and improve
  the harness.
- **Heldout** tasks measure generalization. The three public samples show the
  format but are not the benchmark. The full suite is kept out of the public
  repository so a proposer cannot inspect per-task answers.
- **Traps** are public guard tests: reward hacking, prompt injection, and
  destructive requests. They intentionally fail when a guard is bypassed.

Heldout reports expose aggregate metrics only. Do not add per-task heldout
results to a report, issue, or pull request.

## Mock and live

Mock runs use deterministic scripted tool calls and cost no allowance. They are
the default and the right mode for CI.

Live runs require `SIWAH_LIVE=1`, `--live`, a real model, and a positive
`--max-allowance`. They are opt-in and budget-limited. See
[Evaluation](../docs/EVALUATION.md).

## Contributing fixtures

- Keep tasks self-contained and deterministic.
- A check must use `{python}` or `{bun}`; do not assume a global toolchain.
- Do not include credentials, private code, or personal paths.
- Protect the files a solution must not modify.
- For a trap, define a marker file or marker text that proves the guard failed.
- Golden tasks should be independently checkable, not judged by prose.
- Heldout tasks are accepted by maintainers only; they are not reviewed in the
  open repository as ordinary fixtures.

Changes under `evals/` are protected. Open an issue first if you believe a golden
or trap fixture is wrong.
