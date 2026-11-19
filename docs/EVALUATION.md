# Evaluation

Evaluation is how siwah measures a harness change before promoting it. The
runner copies each task into a fresh temporary workspace, runs the agent, checks
the result, and records metrics. It never exposes the task JSON to the agent.

Evaluation is also used directly from the CLI:

```bash
siwah eval --suite golden
siwah eval --suite golden --baseline
siwah eval --suite traps
```

## Suites

| Suite | Contents | Visibility |
|---|---|---|
| `golden` | 30 tasks across bugfix, feature, refactor, tests, multi-file, comprehension, and tool hygiene; Python and TypeScript; easy/medium/hard | Public |
| `heldout` | Tasks reserved for aggregate scoring; the public repository ships 3 samples | Full suite distributed separately |
| `traps` | Reward hacking, prompt injection, and destructive requests | Public |

A golden task is independently checkable. A heldout task measures generalization
and is reported only in aggregate, so a proposer cannot inspect per-task rows and
optimize against them. A trap task has a marker file or marker text; triggering
it fails the task and is reported as a guard violation or trap.

The public heldout samples exist so the task format is inspectable. They are not
a substitute for the full benchmark, and tuning on them tells you little.

## Where tasks come from

The runner resolves the eval root in this order:

1. `--eval-dir <path>`;
2. `SIWAH_EVAL_DIR`;
3. `<source-checkout>/evals`;
4. `./evals` in the current directory.

The tree must contain the suite directory you request:

```text
evals/
├─ golden/
├─ heldout/
├─ traps/
└─ baseline/
```

A wheel install does not include this tree. Evaluation and evolution therefore
need a source checkout or an explicit eval directory.

When the full heldout suite is distributed separately, point
`SIWAH_HELDOUT_DIR` at the directory containing the heldout task JSON files:

```bash
SIWAH_HELDOUT_DIR=/path/to/heldout siwah eval --suite heldout
```

Alternatively, point `SIWAH_EVAL_DIR` at a complete tree that contains
`golden/`, `heldout/`, and `traps/`. Use a directory you trust: task files are
data, but a malicious task could still influence the run through its prompt or
fixtures.

## Tiers

| Tier | Selection | Use |
|---|---|---|
| `t0` | Structural validation only | Fast schema and guard check; no quality claim |
| `t1` | First six tasks | Screening before a larger spend |
| `t2` | Up to twelve category-relevant tasks | Gate evaluation |
| `t3` | The whole selected suite | Full benchmark and final promotion |

`--limit N` applies after tier selection. `--only id1,id2` selects explicit task
IDs. `--runs N` repeats each task (1–10) for stability.

```bash
siwah eval --suite golden --tier t0
siwah eval --suite golden --tier t2 --limit 8
siwah eval --suite golden --only golden-bugfix-01,golden-ts-01
siwah eval --suite golden --runs 3
```

## Mock and live evaluation

### Mock

The default is a deterministic mock model with scripted tool actions. It costs no
allowance, needs no credentials, and is the right mode for CI and harness
regression tests.

```bash
siwah eval --suite golden
siwah eval --suite golden --baseline
```

Mock measurements are synthetic. They cannot calibrate a provider budget and
they are not evidence that a live model will solve the task.

### Live

Live evaluation uses a real provider. It is opt-in and budget-limited:

```bash
SIWAH_LIVE=1 siwah eval --suite golden --live \
  --model opencode-go/<model-id> \
  --max-allowance 0.05 \
  --max-steps 16
```

Requirements:

- `SIWAH_LIVE=1` in the environment;
- `--live`;
- `--model`, or a configured `[roles].main` model (and a
  `SIWAH_LIVE_MODEL` fallback);
- a positive `--max-allowance`;
- a provider credential.

The default step budget is 8, which is a screening budget. Raise `--max-steps`
for a fuller live measurement. A run stopped at the allowance cap is marked
`stopped_early` and cannot calibrate the budget.

When `--model` is omitted, `SIWAH_LIVE_MODEL` is consulted first, then the
configured `[roles].main` model. The live run uses the provider credentials in
the user's normal config/auth store; the deterministic mock harness is never
selected for a live run.

## Metrics and reports

Reports include:

- pass rate and passed/total tasks;
- steps, input/output tokens, and cache-read ratio;
- allowance and wall-clock duration;
- tool calls and tool-error rate;
- guard violations and trap triggers;
- per-task rows for golden and traps, and aggregate-only rows for heldout.

Reports are written under `~/.siwah/eval/reports/` by default. Use `--output` to
choose a path and `--json` to print the report to stdout. With `--baseline`, the
naive report is written next to the siwah report with a `-naive` suffix and a
comparison summary is printed.

The baseline cache lives under `~/.siwah/eval/cache/`. It is keyed by harness
hash, model, task, and seed. `--no-cache` forces a fresh baseline.

## Comparing with the naive baseline

```bash
siwah eval --suite golden --baseline
```

`--baseline` runs the fixed naive harness as well and prints deltas for pass
rate and cost. It is only valid with `--harness siwah`. `--harness naive` runs
only the baseline.

This comparison is a sanity check, not a leaderboard. One task or one suite run
does not establish a general ranking.

## Calibrating the budget

```bash
SIWAH_LIVE=1 siwah eval --suite golden --live \
  --model opencode-go/<model-id> --max-allowance 0.20 \
  --write-budget
```

`--write-budget` replaces the `[budget]` table in the config with calibrated
allowance and wall-time ceilings from a complete live run. It preserves the other
sections and comments. It refuses:

- mock measurements;
- a T0 validation run;
- a run stopped at the allowance cap.

The weekly limit is still supplied by the caller, because it is a provider fact:
`--weekly-allowance` or `SIWAH_WEEKLY_ALLOWANCE`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every selected task passed and validation was clean |
| `1` | A task failed, a trap triggered, or validation reported an error |
| `2` | Invalid options, missing eval directory, configuration error, or an exception during the run |

The traps suite exits nonzero when a trap is triggered; the report identifies
the cause.

## Task format

A task is one JSON document with this shape:

```json
{
  "id": "golden-bugfix-01",
  "category": "bugfix",
  "language": "python",
  "difficulty": "easy",
  "prompt": "Fix the failing test.",
  "files": {"src/example.py": "...", "tests/test_example.py": "..."},
  "protected": ["tests/test_example.py"],
  "check": "{python} -m pytest -q",
  "mock": {
    "first_tools": [{"name": "read_file", "arguments": {"path": "src/example.py"}}],
    "repair_tools": [],
    "requires_verifier": true,
    "final": "Fixed."
  }
}
```

Fields:

| Field | Meaning |
|---|---|
| `id` | Lowercase letters, digits, `.`, `_`, `-`; 3–80 characters |
| `category` | `bugfix`, `feature`, `refactor`, `tests`, `multi-file`, `comprehension`, `tool-hygiene`, or `trap` |
| `language` | `python` or `typescript` |
| `difficulty` | `easy`, `medium`, or `hard` |
| `prompt` | The user request |
| `files` | Initial workspace files |
| `protected` | Files whose modification fails the task |
| `check` | Shell check using `{python}` or `{bun}` |
| `check_text` | Optional textual check instead of a command |
| `mock` | Scripted deterministic actions for mock runs |
| `trap` | Optional marker for a trap task |

Task files must not contain credential-like values. Unsafe paths are rejected.

## Reproducing a run

Use the same suite, tier, only-list, seed, model, and step budget:

```bash
siwah eval --suite golden --tier t2 --seed 7 --runs 3 \
  --only golden-bugfix-01,golden-ts-01 --json
```

For live runs, also record the model and allowance cap. Provider behavior and
prices change; a report is a snapshot, not a permanent fact.

## Limitations

- Mock results are deterministic but synthetic.
- Heldout aggregate scores are only as meaningful as the hidden suite.
- A single run has variance, especially live; `--runs` helps but does not remove
  it.
- The public samples are not the full heldout benchmark.
- Evaluation is not a security sandbox. Task prompts and fixtures are untrusted
  data; run evaluation in an isolated environment when in doubt.
- Reports are local files. They are not uploaded anywhere.
