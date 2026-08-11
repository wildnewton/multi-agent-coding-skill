---
name: multi-agent-coding
description: Orchestrate a tests-first coding workflow with Coordinator as the semantic routing hub and Hermes as the mechanical dispatcher/verifier.
version: 0.2.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

## When to Use

Use this skill when the user asks Hermes to implement a code change using the multi-agent coding workflow.

Coordinator is the only semantic routing hub. Testing always returns to Coordinator. Review always returns to Coordinator. Hermes does not choose the next specialist agent; Hermes verifies evidence, enforces allowed transitions, and executes Coordinator's routing decision.

The logical communication graph is:

```text
User <-> Coordinator
          <-> Testing
          <-> Review
```

The physical transport is always through Hermes:

```text
Coordinator -> Hermes -> Testing -> Hermes -> Coordinator
Coordinator -> Hermes -> Review  -> Hermes -> Coordinator
```

Testing and Review never route directly to each other and never choose the next agent.

This MVP remains sequential. Do not add parallel execution, webhooks, a database, generic adapters, or automatic merge behavior.

## Preconditions

1. The target repository is available locally and has a clean worktree.
2. `codex` is installed and authenticated.
3. `gh` is installed and authenticated with permission to create/comment on pull requests in the target repository.
4. Create and switch to a dedicated feature branch before any code change.
5. Locate the directory containing this `SKILL.md`; `run_codex.py` and `prompts/` are sibling paths.
6. Choose a stable workflow id, normally `issue-<number>` or `pr-<number>`.

## State Machine

Hermes starts or resumes Coordinator. Only Coordinator may request one of these semantic transitions:

```text
Coordinator
  |-- HANDOFF -> Testing
  |                |
  |                +---- result + verification ----> Coordinator
  |
  |-- HANDOFF -> Review
  |                |
  |                +---- result -------------------> Coordinator
  |
  |-- AWAIT_USER_DECISION -> User response -> Coordinator
  |
  |-- AWAIT_USER_MERGE -> merge gate -> User
  |
  +-- BLOCKED -> stop and report
```

Hermes may reject an invalid or unverified transition, but Hermes must not replace it with a different specialist. Return the verification failure/evidence to Coordinator so Coordinator decides what happens next.

## Procedure

### 1. Enter through Coordinator

Invoke Coordinator first with the user request, acceptance criteria, current repository state, and any existing PR/workflow context:

```bash
python3 <skill-dir>/run_codex.py \
  --agent coordinator \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<user request, acceptance criteria, and current workflow evidence>'
```

For a normal code change, Coordinator should first return a `HANDOFF` to `testing` with a concrete RED task. Coordinator may instead return `AWAIT_USER_DECISION` if a requirement must be resolved before safe test design.

### 2. Execute Coordinator -> Testing

When Coordinator returns:

```json
{"status":"HANDOFF","next_agent":"testing","task":"..."}
```

Hermes validates that the destination is Testing and invokes Testing with exactly the Coordinator-supplied task plus necessary repository/workflow context.

Testing owns test intent. It may modify tests, fixtures, or test-only helpers only.

When Testing returns `RED_COMPLETE`, Hermes verifies mechanically:

- the reported RED commit exists and is current;
- changed files are tests/fixtures only for the target repository;
- the reported targeted test command fails for the expected missing behavior.

If no PR exists yet, push the valid RED branch and open a draft PR.

Publish the Testing handoff comment described below. Then resume Coordinator with:

- Testing's `HERMES_RESULT`;
- RED verification success/failure and evidence;
- current HEAD/PR state.

If Testing returns `BLOCKED`, return that result to Coordinator as well. Testing never chooses Review or the user.

### 3. Coordinator decides after Testing

Coordinator inspects the Testing result and Hermes verification evidence.

Coordinator may:

- hand off to Testing again when coverage is missing, incorrect, or needs clarification;
- implement/fix production code when RED intent is sound;
- return `AWAIT_USER_DECISION` when requirements cannot be safely inferred;
- after implementation, targeted/full test execution, and a GREEN commit, request Review with `HANDOFF -> review`.

A Coordinator handoff to Review must include structured GREEN evidence: `commit`, `test_command`, and `full_test_command`.

Before Hermes executes a Coordinator handoff to Review, Hermes verifies mechanically that the proposed GREEN state is reviewable:

- the reported GREEN `commit` exists and equals current `HEAD`;
- there are no uncommitted tracked/staged changes (`git diff --quiet` and `git diff --cached --quiet`);
- RED test intent was not silently weakened;
- the reported targeted `test_command` passes;
- the reported `full_test_command` passes, or explicitly records why no full suite is available;
- CI passes when configured.

If GREEN verification fails, do not invoke Review. Resume Coordinator with the failed verification evidence. Coordinator decides whether to fix implementation or route back to Testing.

If GREEN verification succeeds, publish the Coordinator handoff comment and invoke a fresh Review.

### 4. Execute Coordinator -> Review

Review is intentionally a new Codex session every time:

```bash
python3 <skill-dir>/run_codex.py \
  --agent review \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<Coordinator review task plus current HEAD, acceptance criteria, RED evidence, PR description, and diff>'
```

Review must not modify files and must not choose the next agent.

For every valid Review result (`REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`):

1. Publish the Review handoff comment.
2. Resume Coordinator with the Review result, reviewed HEAD, and current repository state.

`CHANGES_REQUIRED` does not automatically stop the workflow. Coordinator decides whether to:

- fix an implementation defect itself, then commit/retest and request another fresh Review;
- route missing/incorrect test coverage to Testing;
- ask the user for a decision;
- take another justified action within its role.

### 5. Coordinator -> User decision

When Coordinator returns `AWAIT_USER_DECISION`, Hermes asks the specific question and stops specialist execution. `AWAIT_USER_DECISION` must contain a non-empty `question`. When the user answers, resume the same Coordinator session with that answer and current workflow state.

Testing and Review must not ask the user directly as a routing decision.

### 6. Merge Gate

Only Coordinator may declare `AWAIT_USER_MERGE`, and the result must contain `reviewed_head`.

Before asking the user to merge, Hermes verifies mechanically:

- Review returned `REVIEW_CLEAN` for `reviewed_head`;
- current HEAD still equals `reviewed_head`;
- no tracked/staged changes have appeared since Review;
- required targeted/full tests or CI are passing;
- the PR description reflects the actual implementation and test evidence.

If this gate fails, resume Coordinator with the failed evidence. Do not independently route to Testing or Review.

If the gate passes:

1. Publish the Coordinator merge-ready handoff comment.
2. Tell the user the PR is ready and identify the reviewed HEAD SHA.
3. Ask whether to merge.
4. Never merge without explicit user approval.

## Hermes Routing Rules

Hermes is the orchestrator/transport layer, not the semantic workflow owner.

Hermes must:

- invoke the agent requested by a valid Coordinator `HANDOFF`;
- allow Coordinator `HANDOFF` destinations only to `testing` or `review`;
- require a concrete `task` for every Coordinator `HANDOFF`;
- require `commit`, `test_command`, and `full_test_command` before a Coordinator handoff to Review;
- require a `question` for `AWAIT_USER_DECISION` and `reviewed_head` for `AWAIT_USER_MERGE`;
- reject specialist results that attempt to specify `next_agent`;
- return every Testing and Review result to Coordinator;
- perform deterministic RED/GREEN/merge-gate checks;
- return failed verification evidence to Coordinator rather than selecting a replacement destination;
- preserve Coordinator and Testing sessions per workflow while keeping Review fresh.

Hermes must not infer `Testing -> Review`, `Review -> Testing`, or `Review -> User` transitions on its own.

## PR Handoff Comments

Hermes publishes the handoff comment only after verification of the agent result and the relevant repository/test evidence. Agents must not post their own handoff comments. Their `HERMES_RESULT` is a machine handoff to Hermes; the PR comment is Hermes's human-readable, verified audit trail.

Publish one new top-level PR Conversation comment for each completed/verified handoff once a PR exists. Do not edit a previous handoff comment into the next one.

Render the appropriate template below into text, then from the target repository publish it with the authenticated GitHub CLI:

```bash
printf '%s\n' "$handoff_comment" | gh pr comment <pr-number> --body-file -
```

### Testing handoff

```text
### Testing handoff
Status: <RED verified | verification failed | BLOCKED>
Commit: <red-sha or n/a>
Verification: <RED evidence or failure evidence>
Next: Coordinator
```

### Coordinator handoff

For a specialist route:

```text
### Coordinator handoff
Status: HANDOFF
Commit: <current-head>
Verification: <relevant RED/GREEN evidence>
Decision: <reason for routing>
Next: <Testing | Review>
```

For a merge-ready decision:

```text
### Coordinator handoff
Status: AWAIT_USER_MERGE
Commit: <current-head>
Verification: Review clean for current HEAD; required tests/CI pass.
Next: User merge decision
```

### Review handoff

```text
### Review handoff
Status: <REVIEW_CLEAN | CHANGES_REQUIRED | BLOCKED>
Reviewed HEAD: <head-sha>
Verification: fresh Review session checked the current HEAD against acceptance criteria, RED tests, PR description, and relevant diff.
Findings: <confirmed findings or none>
Next: Coordinator
```

Do not publish a successful verification claim when Hermes's mechanical verification failed. Publish the actual failure evidence when an audit comment is appropriate, then return control to Coordinator.

## Agent Session Policy

- Coordinator: persistent Codex session per workflow id and central semantic context.
- Testing: persistent Codex session per workflow id for test intent/history.
- Review: always fresh; no session id is persisted.
- Agent identity is the role name, not the Codex thread id.

Runtime state is stored by `run_codex.py` under `<skill-dir>/state/<workflow-id>.json` unless `--state-file` is supplied.

## Result Contract

Each agent must finish with exactly one line beginning with:

```text
HERMES_RESULT={...}
```

Expected statuses:

- Coordinator: `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, or `BLOCKED`.
- Testing: `RED_COMPLETE` or `BLOCKED`.
- Review: `REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`.

Coordinator `HANDOFF` must include `next_agent` (`testing` or `review`) and a non-empty `task`. A Review handoff must also include non-empty `commit`, `test_command`, and `full_test_command` evidence fields.

`AWAIT_USER_DECISION` must include a non-empty `question`. `AWAIT_USER_MERGE` must include a non-empty `reviewed_head`.

Testing and Review must not include `next_agent`.

Treat missing, malformed, unexpected, role-incompatible, or contradictory results as failures. Do not infer success or routing from prose.

## Pitfalls

- Do not hard-code `Testing -> Coordinator -> Review` as a one-way sequence.
- Do not let Hermes choose a specialist because a result "looks like" it belongs there.
- Do not let Testing implement production code.
- Do not let Review modify files or directly contact Testing.
- Do not let Coordinator silently rewrite or weaken RED test intent; route test corrections to Testing.
- Do not request Review from an uncommitted worktree.
- Do not resume a Review session; fresh context is deliberate.
- Do not trust an agent's success claim without deterministic verification.
- Do not invoke multiple coding agents concurrently against the same worktree in this MVP.
- Do not let agents publish their own PR handoff comments; Hermes owns the verified audit trail.
- Do not auto-merge.

## Verification

For this skill repository itself, run:

```bash
python3 -m unittest discover -s tests -v
```

For an end-to-end smoke test, choose a small real issue and confirm that Coordinator can route Testing -> Coordinator loops, Review -> Coordinator loops, and Coordinator -> Testing rework without the user sending `your turn` between phases, while each PR-visible verified handoff leaves its own audit comment.
