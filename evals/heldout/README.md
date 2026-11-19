# Heldout samples

This directory contains **three public samples** of the heldout suite. They
exist so contributors and users can inspect the task format and run an aggregate
check locally.

They are not the benchmark.

## What is public and what is not

- Public: three sample task JSON files, the schema, and the runner behavior.
- Not public: the full heldout suite, which is distributed separately to keep the
  benchmark meaningful.
- Reports for the heldout suite expose aggregate metrics only. Per-task rows are
  intentionally omitted.

Do not tune a harness, prompt, or evolution candidate against these samples and
call it generalization. The samples are too small and too visible for that.

## Running

From a source checkout:

```bash
uv run siwah eval --suite heldout
```

With a separately distributed suite:

```bash
SIWAH_HELDOUT_DIR=/path/to/heldout uv run siwah eval --suite heldout
```

You can also point `SIWAH_EVAL_DIR` at a complete tree that contains `golden/`,
`heldout/`, and `traps/`:

```bash
SIWAH_EVAL_DIR=/path/to/evals uv run siwah eval --suite heldout
```

`--eval-dir` takes precedence over the environment variable. The full suite is
not included in a wheel install.

## Why aggregate-only

A heldout suite measures whether a change generalizes beyond the tasks used to
design it. If per-task prompts, answers, and checks were public, an automated
proposer could optimize directly against them. Aggregate reporting keeps the
signal useful.

This is not a claim that the hidden suite is secret in a cryptographic sense. It
is a process control: keep it out of the public repository, restrict access, and
do not paste per-task output into issues or PRs.

## Task format

The format is the same as golden and traps. See
[Evaluation](../../docs/EVALUATION.md#task-format) and the sample files in this
directory. A task defines initial files, a prompt, protected files, a check, and
optional mock actions.

## Contributing

Heldout tasks are accepted by maintainers only. Do not open a pull request that
adds a heldout task with its answer, expected solution, or per-task results. If
you have a candidate task, contact the maintainers first.
