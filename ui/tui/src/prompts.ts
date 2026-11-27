/** Built-in prompt templates owned by siwah. */

const INIT_TEMPLATE = `Create or update \`AGENTS.md\` at {{PATH}}.

Write a compact operating guide for future siwah sessions in this repository.
Every line must help an agent avoid a real mistake or ramp up faster; omit
anything that is already obvious from filenames, language defaults, or a
standard README.

User focus or constraints (honor these):
{{ARGUMENTS}}

## Investigate before writing

Read the highest-value sources first:
- README and root manifests, workspace config, lockfiles
- build, test, lint, formatter, typecheck, migration, and codegen config
- CI workflows, pre-commit hooks, task runners, and release scripts
- existing \`AGENTS.md\`, \`CLAUDE.md\`, editor rules, and project config
- a small number of representative entrypoint files if the architecture is
  still unclear

Prefer executable sources of truth (scripts, config, tests) over prose. If
documentation conflicts with a script, trust the script and keep only what you
can verify.

If \`AGENTS.md\` already exists, improve it in place. Preserve verified useful
guidance, remove stale or speculative claims, and do not rewrite it blindly.

## What belongs in the file

- Exact commands an agent would otherwise guess wrong, including focused test
  and single-package commands.
- Required command order when it matters, such as lint, typecheck, then test.
- Real package boundaries, entrypoints, generated files, and files that must
  change together.
- Toolchain or environment quirks: env loading, migrations, codegen, dev
  servers, fixtures, snapshots, flaky suites, required services.
- Project conventions that differ from language or framework defaults.
- References to existing instruction files that matter.

Keep it short and repo-specific. Exclude generic advice, long tutorials,
obvious language rules, speculative claims, and content that belongs in a
separate linked file. When in doubt, leave it out.

## Project memory

Also create or update \`.siwah/project.md\` with the same evidence rules:
a short overview and main entry points, the exact test/lint/typecheck/build
commands, and conventions that differ from language or framework defaults.
If the file exists, improve it in place instead of replacing verified facts.

After updating both files, reply with a short summary of what changed and any
important uncertainty you could not verify. Do not claim a command works
unless you ran it or found it in executable configuration.`

const REVIEW_TEMPLATE = `Review code changes in {{PATH}} and report actionable findings.

Input: {{ARGUMENTS}}

## Scope

Choose the review target from the input:
- no input: uncommitted and untracked changes
- a commit hash: that commit
- a branch: changes from that branch to HEAD
- a pull request reference: its diff and context

Use git, the repository files, and the existing test/lint/typecheck commands.
Review only the requested changes, not unrelated pre-existing code.

## Gather context

Diffs alone are not enough. Read the complete changed files and the code
around each hunk. Check callers, configuration, tests, and conventions before
calling something a bug. Prefer executable evidence over assumptions.

## Priorities

1. Bugs and security issues: incorrect control flow, edge cases, race
   conditions, data loss, auth bypass, injection, swallowed failures.
2. Behavioral regressions and broken compatibility.
3. Missing tests for a risky change.
4. Structural problems that make the change hard to maintain.
5. Performance only when the problem is concrete and material.

Do not inflate severity. For every finding, state the triggering scenario and
why it matters. If you are not certain, say so and explain what evidence is
missing. Do not report style preferences as defects.

## Output

Return findings first, ordered by severity. Include file and line references
when possible. Then list verification you ran and any remaining gaps. If there
are no findings, say that directly.`

const LEARN_TEMPLATE = `Review the work in this session and turn it into durable learning.

Focus or constraints: {{ARGUMENTS}}

## Decide the artifact

1. If the session produced a repeatable procedure, create or update one skill
   under the harness with \`skill_manage\`. Check existing skills first and
   extend a matching skill instead of creating a near-duplicate.
2. If it produced a durable but non-procedural insight, record one concise
   lesson with \`lesson_write\`: cue, rule, evidence, confidence, and target
   (project for this repository, user for cross-project preferences).
3. If neither applies, say so and do not create filler.

## Quality rules

- Use only evidence present in the session; never invent commands or results.
- Remove secrets, credentials, personal data, and one-off task details.
- Keep a skill description short and make its trigger explicit.
- Prefer a script or reference file when the skill would otherwise contain a
  large body of code or documentation.
- Do not weaken permissions, verifier checks, or protected paths.

Finish by naming the skill or lesson created and why it is reusable.`

function fill(template: string, path: string, argumentsText: string): string {
  return template.split("{{PATH}}").join(path).split("{{ARGUMENTS}}").join(argumentsText.trim())
}

export function initPrompt(path: string, argumentsText = ""): string {
  return fill(INIT_TEMPLATE, path, argumentsText)
}

export function reviewPrompt(path: string, argumentsText = ""): string {
  return fill(REVIEW_TEMPLATE, path, argumentsText)
}

export function learnPrompt(argumentsText = ""): string {
  return LEARN_TEMPLATE.split("{{ARGUMENTS}}").join(argumentsText.trim())
}
