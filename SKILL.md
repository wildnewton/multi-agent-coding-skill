---
name: multi-agent-coding
description: Orchestrate a tests-first coding workflow with Coordinator as the semantic routing hub and Hermes as the mechanical dispatcher/verifier.
version: 0.3.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

## When to Use

Use this skill when the user asks Hermes to implement a code change using the multi-agent coding workflow.

Coordinator, Testing, and Review are separate Codex CLI sessions orchestrated by Hermes.

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
3. `gh` is installed and authenticated with permission to create, comment on, and update pull requests in the target repository. Merge permission is required only if the user asks Hermes to perform the final merge.
4. Create and switch to a dedicated feature branch before any code change.
5. Locate the directory containing this `SKILL.md`; `run_codex.py` and `prompts/` are sibling paths.
6. Choose a stable workflow id, normally `issue-<number>` or `pr-<number>`.

## Git Ownership Invariant

Hermes owns all repository-state mutation:

- branch creation/switching;
- `git add`, commit, push, restore/reset/clean/rebase/merge when required;
- PR creation and metadata updates;
- PR handoff comments;
- marking a draft PR ready;
- final merge after explicit user approval.

Codex agents may use read-only commands such as `git status`, `git diff`, `git log`, `git show`, `git rev-parse`, and read-only `gh` queries. They must not mutate `.git/` or remote state, including via `gh api`/GitHub Git Data API workarounds.

Git/GitHub mutation is outside every agent's responsibility; inability to perform it is not a valid `BLOCKED` reason.

Before every Codex invocation, the worktree must be clean. `run_codex.py` enforces this.

Coordinator and Testing may leave only role-permitted unstaged file edits. Review must leave the worktree unchanged. Hermes validates the resulting state and either commits permitted verified edits or discards unverified edits before another Codex invocation.

If an agent returns `BLOCKED`, an invalid result, or fails mechanical verification after leaving edits, Hermes must not complete the agent's work on its behalf. Record the evidence, discard the edits created since the clean pre-invocation state, restore a clean HEAD, and return the failure to Coordinator.

Agents must not include `commit` in `HERMES_RESULT`; a commit SHA becomes authoritative only after Hermes creates/verifies it.

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
{"status":"HANDOFF","next_agent":"testing","task":"...","reason":"..."}
```

Hermes validates that the destination is Testing and that `task` and `reason` are non-empty. If a PR already exists, publish the descriptive Coordinator -> Testing handoff comment before dispatch. Then invoke Testing with exactly the Coordinator-supplied task plus necessary repository/workflow context.

Testing owns test intent. It may modify tests, fixtures, or test-only helpers only, and leaves permitted edits unstaged for Hermes.

When Testing returns `RED_COMPLETE`, Hermes verifies mechanically:

- changed files are tests/fixtures/test-only helpers only for the target repository;
- the reported targeted test command fails for the expected missing behavior, not because of broken test setup or unrelated failure.

If RED verification passes, Hermes creates the RED commit from the validated test paths, pushes it, and records the resulting SHA.

If no PR exists yet, open a draft PR after the first valid RED commit. Because the initial Coordinator handoff happened before a PR existed, publish the retained initial Coordinator -> Testing decision followed by the Testing -> Coordinator RED handoff so the audit trail remains complete.

Publish the Testing handoff comment described below. Then resume Coordinator with:

- Testing's `HERMES_RESULT`;
- the Hermes-created RED commit SHA;
- RED verification success/failure and evidence;
- current HEAD/PR state.

If Testing returns `BLOCKED`, return that result to Coordinator as well. Testing never chooses Review or the user.

### 3. Coordinator decides after Testing

Coordinator inspects the Testing result and Hermes verification evidence.

Coordinator may:

- hand off to Testing again when coverage is missing, incorrect, or needs clarification;
- implement/fix production code when RED intent is sound;
- return `AWAIT_USER_DECISION` when requirements cannot be safely inferred;
- after implementation and targeted/full test execution, request Review with `HANDOFF -> review`; Hermes creates the GREEN commit after verification.

A Coordinator handoff to Review must include a non-empty `reason` and exactly one of `full_test_command` or `full_test_unavailable_reason`. The targeted command comes from the verified Testing result.

Before Hermes executes a Coordinator handoff to Review, Hermes verifies mechanically that the proposed GREEN state is reviewable:

- changes are within the intended implementation scope;
- no staged changes were created by Coordinator;
- RED test intent was not silently weakened;
- the latest verified targeted `test_command` from Testing passes;
- if `full_test_command` is present, it passes;
- otherwise `full_test_unavailable_reason` must clearly state why no full suite is available.

If local GREEN verification fails, do not invoke Review. Discard the unverified implementation edits back to the clean pre-invocation HEAD and resume Coordinator with the failed verification evidence. Coordinator decides whether to fix implementation or route back to Testing.

If local GREEN verification succeeds, Hermes creates and pushes the GREEN commit, waits for configured CI and requires it to pass, and updates the PR description to reflect the actual RED/GREEN state. If push or CI verification fails, do not invoke Review; resume Coordinator with the failed verification evidence at the clean committed HEAD.

If GREEN verification succeeds, publish the descriptive Coordinator -> Review handoff comment with the GREEN commit/test/CI evidence and invoke a fresh Review.

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
2. If Review returned `REVIEW_CLEAN`, update the PR description to reflect completed Review and mark a draft PR ready (`draft=false`) before resuming Coordinator.
3. Resume Coordinator with the Review result, reviewed HEAD, and current repository/PR state.

`CHANGES_REQUIRED` does not automatically stop the workflow. Coordinator decides whether to:

- fix an implementation defect itself, then leave the edits unstaged for Hermes to validate/commit/retest before requesting another fresh Review;
- route missing/incorrect test coverage to Testing;
- ask the user for a decision;
- take another justified action within its role.

### 5. Coordinator -> User decision

When Coordinator returns `AWAIT_USER_DECISION`, Hermes asks the specific question and stops specialist execution. `AWAIT_USER_DECISION` must contain a non-empty `question`. When the user answers, resume the same Coordinator session with that answer and current workflow state.

Testing and Review must not ask the user directly as a routing decision.

### 6. Merge Gate

Only Coordinator may declare `AWAIT_USER_MERGE`, and the result must contain `reviewed_head` and explicit `draft=false`.

Before asking the user to merge, Hermes verifies mechanically:

- Review returned `REVIEW_CLEAN` for `reviewed_head`;
- current HEAD still equals `reviewed_head`;
- no tracked/staged/untracked changes have appeared since Review (`git status --porcelain` is empty);
- required targeted/full tests and configured CI are passing;
- the PR description reflects the actual implementation, test evidence, and Review status;
- GitHub reports `draft=false`.

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
- require a concrete `task` and non-empty `reason` for every Coordinator `HANDOFF`;
- require exactly one of `full_test_command` or `full_test_unavailable_reason` before a Coordinator handoff to Review;
- require a `question` for `AWAIT_USER_DECISION` and `reviewed_head` plus `draft=false` for `AWAIT_USER_MERGE`;
- reject any agent result that includes `commit`;
- reject specialist results that attempt to specify `next_agent`;
- return every Testing and Review result to Coordinator;
- perform deterministic RED/GREEN/merge-gate checks;
- return failed verification evidence to Coordinator rather than selecting a replacement destination;
- preserve Coordinator and Testing sessions per workflow while keeping Review fresh;
- require a clean worktree before every Codex invocation;
- create and push verified RED/GREEN commits itself;
- own PR metadata updates, handoff comments, and draft/ready transitions.

Hermes must not infer `Testing -> Review`, `Review -> Testing`, or `Review -> User` transitions on its own.

## PR Handoff Comments

Hermes publishes the handoff comment only after verification of the agent result and the relevant repository/test evidence. Agents must not post their own handoff comments. Their `HERMES_RESULT` is a machine handoff to Hermes; the PR comment is Hermes's human-readable, verified audit trail.

Handoff comments must be descriptive enough to show why the transition happened and what the next agent is being asked to do.

Publish one new top-level PR Conversation comment for each completed/verified handoff once a PR exists. Do not edit a previous handoff comment into the next one. For Coordinator specialist handoffs, publish the comment before specialist dispatch; backfill the initial pre-PR Coordinator -> Testing handoff immediately after the first valid RED opens the draft PR.

Render the appropriate template below into text, then from the target repository publish it with the authenticated GitHub CLI:

```bash
printf '%s\n' "$handoff_comment" | gh pr comment <pr-number> --body-file -
```

### Coordinator -> Testing handoff

```text
### Coordinator -> Testing handoff
Status: HANDOFF
HEAD at decision: <sha>
Decision: <why Testing is needed>
Task: <specific behavior/test gap to address>
Evidence: <acceptance criterion, Review finding, live evidence, or prior RED gap>
Next: Testing
```

### Testing -> Coordinator handoff

```text
### Testing -> Coordinator handoff
Status: <RED verified | verification failed | BLOCKED>
RED commit: <Hermes-created sha or n/a>
Test command: <targeted command or n/a>
Coverage: <behaviors specified>
Verification: <why RED is valid or exact failure>
Next: Coordinator
```

### Coordinator -> Review handoff

```text
### Coordinator -> Review handoff
Status: HANDOFF
GREEN commit: <Hermes-created sha>
Decision: <why implementation is ready for independent Review>
Review task: <specific scope>
Verification: <targeted/full test and CI evidence>
Evidence: <relevant acceptance criteria / RED history>
Next: Review
```

### Review -> Coordinator handoff

```text
### Review -> Coordinator handoff
Status: <REVIEW_CLEAN | CHANGES_REQUIRED | BLOCKED>
Reviewed HEAD: <sha>
Scope: <what was reviewed>
Findings: <confirmed findings or none>
Verification: <fresh Review session / PR / CI evidence>
Next: Coordinator
```

### Coordinator -> User merge gate

```text
### Coordinator -> User merge gate
Status: AWAIT_USER_MERGE
Reviewed HEAD: <sha>
Draft: false
Decision: <why the reviewed PR is ready>
Verification: <Review + tests + CI + clean worktree + PR metadata>
Next: User merge decision
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

Coordinator `HANDOFF` must include `next_agent` (`testing` or `review`), a non-empty `task`, and a non-empty `reason`. A Review handoff must also include exactly one of `full_test_command` or `full_test_unavailable_reason`.

Testing `RED_COMPLETE` must include a non-empty `test_command`.

`AWAIT_USER_DECISION` must include a non-empty `question`. `AWAIT_USER_MERGE` must include a non-empty `reviewed_head` and explicit `draft=false`.

No agent may include `commit`. Testing and Review must not include `next_agent`.

Treat missing, malformed, unexpected, role-incompatible, or contradictory results as failures. Do not infer success or routing from prose.

## Pitfalls

- Do not hard-code `Testing -> Coordinator -> Review` as a one-way sequence.
- Do not let Hermes choose a specialist because a result "looks like" it belongs there.
- Do not let Testing implement production code.
- Do not let Review modify files or directly contact Testing.
- Do not let Coordinator silently rewrite or weaken RED test intent; route test corrections to Testing.
- Do not invoke Review before Hermes has created and pushed the verified GREEN commit.
- Do not resume a Review session; fresh context is deliberate.
- Do not trust an agent's success claim without deterministic verification.
- Do not invoke multiple coding agents concurrently against the same worktree in this MVP.
- Do not let agents publish their own PR handoff comments; Hermes owns the verified audit trail.
- Do not auto-merge.
- Do not let agents mutate git or GitHub state, including API-based remote commits.
- Do not start a Codex invocation from a dirty worktree.
- Do not let Hermes rescue a `BLOCKED` agent by completing the agent's domain work.
- Do not leave PR metadata stale after GREEN or Review.
- Do not present `draft=true` as merge-ready.

## Verification

For this skill repository itself, run:

```bash
python3 -m unittest tests/test_run_codex.py tests/test_git_ownership.py -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py
```

For an end-to-end smoke test, choose a small real issue and confirm that Coordinator can route Testing -> Coordinator loops, Review -> Coordinator loops, and Coordinator -> Testing rework without the user sending `your turn` between phases, while each PR-visible verified handoff leaves its own audit comment. Also confirm that each Codex invocation starts from a clean worktree, Hermes-created commits are the visible HEAD, Review sessions remain fresh, and the merge gate cannot pass while the PR is Draft.
