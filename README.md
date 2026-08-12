# Multi-Agent Coding Skill

A Hermes skill for orchestrating a tests-first coding workflow with multiple Codex agents while keeping routing, verification, and merge decisions explicit.

The workflow separates three specialist roles:

- **Coordinator** — the semantic routing hub. It interprets the task, decides what happens next, implements production changes, and is the only agent allowed to route work to another specialist.
- **Testing** — owns test intent and RED-state creation. It may change tests, fixtures, and test-only helpers, but not production code.
- **Review** — performs a fresh, read-only review of the current GREEN state. It reports findings but does not modify files or choose the next agent.

Hermes sits between them as the mechanical dispatcher and verifier. It executes Coordinator-approved transitions, checks repository/test evidence, maintains workflow state, and publishes verified handoff comments to the pull request.

## Why this exists

Multi-agent coding workflows can become unreliable when every agent is allowed to decide what happens next. This project deliberately centralizes semantic routing in Coordinator while leaving deterministic checks to Hermes.

The intended result is a workflow where:

1. requirements are interpreted once by a central coordinator;
2. tests define the missing behavior before implementation;
3. production changes are reviewed only after a mechanically verified GREEN state;
4. failed verification returns evidence to Coordinator instead of triggering hidden routing logic;
5. each verified phase transition can be recorded in the PR as an audit trail;
6. merge remains an explicit user decision.

## Architecture

Logical communication:

```text
User <-> Coordinator
          <-> Testing
          <-> Review
```

Physical transport:

```text
Coordinator -> Hermes -> Testing -> Hermes -> Coordinator
Coordinator -> Hermes -> Review  -> Hermes -> Coordinator
```

Testing and Review never route directly to each other. They always return their result to Coordinator, which decides the next semantic step.

## Workflow

The current MVP is intentionally sequential.

```text
User request
    |
    v
Coordinator
    |
    | HANDOFF -> Testing
    v
Testing creates RED test intent
    |
    v
Hermes verifies RED
    |
    v
Coordinator implements/fixes production code
    |
    v
Hermes verifies committed GREEN state
    |
    | HANDOFF -> Review
    v
Fresh Review session
    |
    v
Coordinator
    |
    +--> fix implementation
    +--> HANDOFF -> Testing for test rework
    +--> AWAIT_USER_DECISION
    +--> AWAIT_USER_MERGE
```

A `CHANGES_REQUIRED` review is not an automatic stop or an automatic route back to Testing. Review returns evidence to Coordinator, and Coordinator decides whether the problem is implementation, test coverage, requirements, or something else.

## Verification gates

Hermes does not trust an agent's success claim by itself. It performs deterministic checks before allowing important transitions.

### RED gate

Before accepting Testing's `RED_COMPLETE`, Hermes verifies that:

- the reported RED commit exists and is current;
- changed files are limited to tests, fixtures, or test-only helpers;
- the targeted test command fails for the expected missing behavior.

### GREEN gate

Before invoking Review, Hermes verifies that:

- the reported GREEN commit exists and equals current `HEAD`;
- there are no uncommitted tracked or staged changes;
- RED test intent was not silently weakened;
- the targeted test command passes;
- the full suite passes when one is available, or Coordinator supplies a reason why no full suite can be run;
- CI passes when configured.

### Merge gate

Before asking the user to merge, Hermes verifies that:

- Review returned `REVIEW_CLEAN` for the reviewed HEAD;
- current HEAD still matches that reviewed commit;
- no tracked or staged changes appeared after review;
- required tests / CI still pass;
- the PR description matches the implementation and test evidence.

The workflow never auto-merges.

## Agent session policy

- **Coordinator** — persistent Codex session per workflow id.
- **Testing** — persistent Codex session per workflow id.
- **Review** — always a fresh Codex session.

Persistent session ids are stored in a JSON state file under `state/<workflow-id>.json` by default. Review deliberately receives fresh context each time so a previous review cannot silently bias a later one.

## Result contract

Every agent invocation must finish with exactly one machine-readable line:

```text
HERMES_RESULT={...}
```

Allowed statuses are role-specific:

| Agent | Allowed statuses |
| --- | --- |
| Coordinator | `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED` |
| Testing | `RED_COMPLETE`, `BLOCKED` |
| Review | `REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED` |

Important constraints:

- Coordinator is the only role allowed to return `next_agent`.
- Coordinator `HANDOFF` may target only `testing` or `review`.
- A handoff to Review must include `commit`, `test_command`, and exactly one of `full_test_command` or `full_test_unavailable_reason`.
- `AWAIT_USER_DECISION` must contain a non-empty `question`.
- `AWAIT_USER_MERGE` must contain `reviewed_head`.
- Testing and Review must not include `next_agent`.
- malformed, contradictory, or role-incompatible results are rejected rather than inferred from prose.

## Requirements

The target coding repository should be available locally with a clean worktree. The workflow also expects:

- Python 3;
- authenticated `codex` CLI;
- authenticated `gh` CLI with permission to create and comment on pull requests in the target repository;
- a dedicated feature branch in the target coding repository before code changes begin.

The skill repository itself does not need to be the repository being modified. `run_codex.py --repo` points each agent at the target coding repository.

## Running an agent

`run_codex.py` is the thin Codex CLI wrapper used by the workflow.

Start or resume Coordinator:

```bash
python3 run_codex.py \
  --agent coordinator \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Implement issue #123. Acceptance criteria: ...'
```

Invoke Testing only when Coordinator has returned a valid `HANDOFF` to `testing`:

```bash
python3 run_codex.py \
  --agent testing \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Add the RED test requested by Coordinator ...'
```

Invoke Review only after Coordinator requests Review and Hermes has verified the GREEN gate:

```bash
python3 run_codex.py \
  --agent review \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Review current HEAD against the acceptance criteria and RED evidence ...'
```

Optional arguments:

- `--state-file <path>` — override the default workflow state file.
- `--prompt-dir <path>` — override the bundled `prompts/` directory.

The wrapper validates agent-specific result contracts and preserves Codex sessions for Coordinator and Testing. It does not itself decide semantic routing.

## PR handoff audit trail

Once a PR exists, Hermes can publish one top-level PR conversation comment for each completed and verified handoff.

The audit trail distinguishes:

- Testing handoff and RED verification;
- Coordinator routing decisions and GREEN evidence;
- Review result and reviewed HEAD;
- Coordinator's merge-ready decision.

Agents do not publish their own handoff comments. Hermes publishes the verified human-readable record after checking the machine result and repository evidence.

## Repository layout

```text
.
├── SKILL.md                 # Full Hermes workflow and routing specification
├── run_codex.py             # Codex session/state/result-contract wrapper
├── prompts/
│   ├── coordinator.md       # Coordinator role contract
│   ├── testing.md           # Testing role contract
│   └── review.md            # Review role contract
├── tests/                   # Runner/workflow regression tests
├── .github/workflows/       # CI for this skill repository
└── README.md
```

`SKILL.md` is the authoritative workflow specification. The README is an overview and operator-oriented entry point rather than a duplicate of every routing rule.

## Testing this repository

Run the full test suite with:

```bash
python3 -m unittest discover -s tests -v
```

For an end-to-end smoke test, use a small real coding issue and verify that the workflow can handle:

- Testing -> Coordinator loops;
- Review -> Coordinator loops;
- Coordinator -> Testing rework;
- user-decision pauses;
- the final user merge gate;
- PR-visible verified handoff comments.

## Design boundaries

This is an MVP. The current design intentionally avoids:

- parallel agent execution against the same worktree;
- Testing -> Review direct routing;
- Review -> Testing direct routing;
- specialist-controlled user routing;
- automatic merge behavior;
- generic agent adapters;
- webhooks or a workflow database.

Keeping these constraints explicit makes the workflow easier to inspect, test, and reason about before adding more concurrency or infrastructure.
