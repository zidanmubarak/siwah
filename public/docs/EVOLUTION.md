# Evolution

siwah can improve its own harness and, under a stricter gate, its own core code.
The design is deliberately conservative: an agent that can edit its evaluator or
its rollback mechanism has no guarantee, so those paths are protected and owned
by the kernel.

Evolution is optional. It is disabled by kill switches, refuses dirty
repositories, and does not run paid live evaluation unless you ask for it.

## What can change

| Level | Scope | Examples | Gate |
|---|---|---|---|
| **0** | Harness data | A prompt, tool guidance, a small skill edit | Candidate safety validation and a T0 smoke probe |
| **1** | Multi-file harness data | Coordinated prompt and skill changes, a subagent spec | T1 screening against a baseline, then a T2 gate |
| **2** | Core code under `src/siwah/` outside protected paths | A bug fix in a tool or provider | Isolated worktree, diff limits, tests, lint, type checks, smoke boot, and T0–T3 evaluation |

Harness data lives under `~/.siwah/harness/` and is a git repository. Allowed
paths include `prompts/`, `tool_guidance/`, `skills/`, `profiles/`,
`subagents/`, `commands/`, `hooks/`, and `config.toml`.

Level 2 rejects changes to:

- `src/siwah/kernel/`;
- `evals/`;
- `ui/`;
- `tests/kernel/`;
- `kernel.manifest.json`;
- other protected bootstrap, config, auth, and profile loaders enforced by the
  gate.

It also rejects large patches (more than five files or 200 changed lines),
removed lines under `tests/`, credential-like content, guard relaxation, and
changes that weaken or delete tests. The full protected inventory is owned by
the kernel, not by the proposer.

## The loop

```text
observe → reflect → propose → probe → gate → compose → promote → canary
```

1. **Observe** recent sessions after redaction.
2. **Reflect** with a model whose profile says it does not train on the data.
3. **Propose** a typed candidate: level, kind, scope, target paths, diff,
   rationale, expected metric, and risk.
4. **Probe** the candidate in an isolated copy of the harness and run the
   evaluation tiers.
5. **Gate** the measured result against the configured thresholds.
6. **Compose** non-conflicting passing candidates and gate the combination.
7. **Promote** with a git commit and tag.
8. **Canary** the promoted revision and record samples.

Level 0–1 activation happens on the next session, not mid-session, so the prompt
cache is not invalidated. Level 2 restarts through the supervisor.

## Before a live cycle

A live cycle needs a provider, a model, an allowance budget, and an explicit
privacy assertion.

PowerShell:

```powershell
$env:SIWAH_LIVE = "1"
siwah models probe opencode-go/<model-id>
siwah models privacy opencode-go/<model-id> --training false
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
```

macOS/Linux:

```bash
export SIWAH_LIVE=1
siwah models probe opencode-go/<model-id>
siwah models privacy opencode-go/<model-id> --training false
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
```

`siwah models probe` reports `training=unmarked` when the provider's catalog does
not expose privacy metadata. Evolution refuses an unmarked model because the
reflector sees redacted session data. Only set `training=false` when the
provider contract confirms that the model does not train on the data. `models
probe` preserves an existing privacy assertion instead of overwriting it.

If a privacy or configuration preflight fails before any model call, the cycle
is not recorded and no cooldown is consumed. Older releases recorded the cycle;
use `siwah evolve --reset-cooldown` once to clear a stuck cooldown. It does not
reset the daily cycle count or recorded allowance.

## Candidate normalization

The proposer is an LLM, so the parser accepts a small amount of schema drift
before rejecting a candidate. For example, `scope: "agent"` is treated as the
global harness, `level: "1"` becomes `1`, and a single evidence sentence becomes
a one-item list. If the JSON is missing, truncated, or still fails schema
validation, the proposer asks the model for one repair with the exact schema.
Proposal calls use a larger output budget so a full-file `changes` value is not
cut off. A second failure ends the cycle with an audit event; no harness change
is applied. Inspect `siwah evolve log` before retrying.

## Probing tiers

| Tier | What it does |
|---|---|
| **T0** | Structural validation and a smoke check; no meaningful quality claim |
| **T1** | A small screening subset (up to six tasks) compared with a baseline |
| **T2** | Up to twelve category-relevant tasks and the gate thresholds |
| **T3** | The full golden suite plus heldout for Level 2 or final promotion |

T1 is a screen: a regression there rejects the candidate before spending on
T2. T2 applies the configured gate. T3 is the broadest measurement.

## Gate policy

```toml
[gate]
golden_regression_tolerance = 0.0
heldout_noninferiority = 0.02
min_gain = "pass_rate_up OR (steps_or_cost_down >= 0.10 with equal pass_rate)"
max_cost_ratio = 1.2
runs_per_task = 3
```

A candidate is promoted only when:

- the golden pass rate does not regress beyond `golden_regression_tolerance`;
- heldout performance is non-inferior within `heldout_noninferiority`;
- `min_gain` is satisfied: either pass rate improves, or steps or cost drop by at
  least 10% with equal pass rate;
- the cost ratio stays at or below `max_cost_ratio`;
- every configured safety and structural check passes.

The gate scanner is conservative but heuristic. It is not a formal policy
verifier, and it cannot prove the absence of every unsafe change.

## Level 0 and Level 1

```bash
siwah evolve --seed
siwah evolve --live --model opencode-go/<model-id> --max-allowance 0.05
siwah evolve --live --seed --probe-limit 2
```

- Without `--live`, only an explicit `--seed` cycle runs. It uses a deterministic
  synthetic reflector, proposer, and prober. This is a cold-start smoke fixture,
  not evidence of coding quality.
- `--live` requires `SIWAH_LIVE=1`, a real model, and a positive
  `--max-allowance`. It copies the harness into a temporary home and runs the
  real evaluation suite.
- `--probe-limit` limits tasks per live probe for a cheap smoke run.
- A promoted harness revision is committed and tagged `evo-N`.

The TUI `/evolve` panel summarizes and groups audit events (candidate, gate
result, pause/resume, rollback), shows whether automatic evolution is paused, and
supports `r` rollback and `p` pause/resume.

## Level 2

```bash
siwah evolve --patch-file fix-edit-file.diff
```

The patch is reviewed in an isolated git worktree. The default gate:

1. checks the protected path inventory and the five-file / 200-line limit;
2. rejects removed test lines and suspicious content;
3. runs `pytest`, `ruff`, `pyright`, a CLI smoke boot, and `siwah eval` at
   T0–T3 in the worktree;
4. requires a one-use kernel approval bound to the checked candidate and HEAD;
5. commits and tags a passing candidate as `evo-core-N`;
6. performs a post-promotion boot check and restarts the supervisor.

A failure attempts a non-discarding reset to the previous commit, deletes the
candidate tag, and audits the rollback. The main repository must be clean before
promotion. A dirty or conflicting rollback is reported as blocked and needs
human conflict resolution.

The main repository must also be clean before promotion. A tag is not a license
to overwrite uncommitted work.

## Budget and kill switches

```toml
[budget]
max_allowance_per_cycle = 1.0
max_cycles_per_day = 3
reserve_weekly_pct = 40
max_concurrency = 2
cooldown_minutes = 60
max_wall_minutes = 45
```

The governor enforces:

- a per-cycle allowance cap;
- a daily cycle count;
- a cooldown between cycles;
- a wall-time limit;
- an optional weekly reserve, supplied by the caller because it is a provider
  fact: `--weekly-allowance <amount>` or `SIWAH_WEEKLY_ALLOWANCE`.

Kill switches:

```bash
siwah evolve --pause          # pause before the next cycle
siwah evolve --resume
```

- `SIWAH_EVOLVE=off` disables evolution entirely.
- `SIWAH_EVOLVE_AUTO=0` disables the automatic post-session trigger.
- The pause file is also honored by the TUI `/pause` and `/resume` actions.

## Automatic trigger

Automatic evolution runs after a session when:

- `[evolution].auto` is enabled;
- the session shows an error, correction, or tool-error signal;
- `min_sessions` is met;
- `cooldown_minutes` has elapsed.

It spawns a separate `siwah evolve` process. It does not opt into paid live
evaluation by itself; without live evaluation it cannot silently promote a
candidate through the synthetic gate.

Cross-process idle admission is not enforced by a shared lease yet. Avoid running
automatic evolution concurrently with interactive work when strict isolation
matters. Keep automatic evolution disabled if you need a strict guarantee that no
cycle starts while you are working.

## Canary and rollback

The canary monitor records tool-error, correction, and abort rates over the first
`[canary].sessions` sessions (default 10). It fails when a rate exceeds the
baseline plus the configured threshold (default 15 percentage points for error
and correction rates).

Rollback options:

```bash
siwah rollback evo-N              # restore a harness tag
siwah rollback                    # restore the latest healthy protected tag
siwah rollback kernel-healthy-<hash12>
```

`siwah rollback` restores only protected paths; the rest of the workspace is
untouched. A dirty or conflicting rollback is refused rather than discarding
your edits.

The canary monitor persists state, but production session sampling is not
connected end-to-end. Automatic post-session rollback is therefore **not a
guarantee**. Use `siwah rollback evo-N` deliberately when a promotion is bad.

## Audit and state

- `~/.siwah/audit.jsonl` stores hash-chained audit events.
- `~/.siwah/budget-state.json` stores cycle and allowance counters.
- Evolution state is persisted separately and exposed by `GET /v1/evolution`.
- `siwah evolve log` shows recent evolution events.
- `siwah audit --limit 20` shows the newest audit events.

Audit events cover observation, reflection, proposal, gate results, promotion,
pause/resume, rollback, and spend. They are data for humans and tooling, not a
replacement for reviewing a diff.

## What is not guaranteed

- The gate is heuristic, not a proof of safety or quality.
- The synthetic seed cycle is a smoke fixture, not a benchmark.
- Canary sampling and automatic rollback are not end-to-end.
- Level 2 recovery from a delayed daemon boot failure is not fully journaled.
  The supervisor handles two consecutive failed boots, but do not infer recovery
  from every delayed failure mode.
- Rendered system prompts are pinned when a session starts; this is not a
  snapshot of all harness and config state.
- Model-specific overlays for complex model IDs may not resolve consistently
  through both the gate and the loader. Validate before relying on one.
- Promotion refuses dirty repositories; it does not merge or rebase your work.
- Evolution is not a substitute for code review, tests, or a sandbox.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `--live requires SIWAH_LIVE=1` | Set it in the same process. PowerShell: `$env:SIWAH_LIVE="1"`; bash: `export SIWAH_LIVE=1`. |
| `SIWAH_LIVE=1 siwah evolve ...` fails in PowerShell | PowerShell does not use leading `NAME=value` assignments. Set `$env:SIWAH_LIVE="1"` first. |
| `--live requires a positive --max-allowance` | Pass a positive per-probe cap, for example `0.05`. |
| `model ... is not marked training=false` | Run `siwah models probe <model>`, then `siwah models privacy <model> --training false` only if the provider contract confirms it. |
| `budget_blocked` / `cooldown active` | Wait for the cooldown, or use `siwah evolve --reset-cooldown` after a failed preflight. Direct paid retries remain rate-limited. |
| `invalid candidate` / `scope` or `evidence` errors | The parser normalizes common drift and retries one schema repair. If it still fails, inspect `siwah evolve log` and report the candidate shape. |
| `Non-seed evolution requires real evaluation` | Use `--seed`, or `--live` with the requirements above. |
| `evolution is paused` | Run `siwah evolve --resume` or remove the pause state. |
| `kernel enforcement integrity check failed` | Run `siwah kernel verify`; restore protected paths before evolving. |
| Patch rejected for size | Level 2 is limited to five files and 200 changed lines. |
| Rollback blocked | Commit or stash conflicting changes, then retry. |
| No live model | Set `[roles].reflector` or `[roles].main`, or pass `--model`. |

See [Evaluation](EVALUATION.md) for the suites and metrics, and
[Security](SECURITY.md) for the trust model.
