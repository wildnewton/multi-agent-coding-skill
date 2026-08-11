---
name: multi-agent-coding
description: Orchestrate a tests-first Codex workflow with Coordinator as the semantic routing hub and Hermes as the git-owning dispatcher/verifier.
version: 0.3.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

## Core Model

Use this skill when the user asks Hermes to implement a code change with the multi-agent coding workflow.

Coordinator, Testing, and Review are separate Codex CLI sessions orchestrated by Hermes.

- Coordinator is the only semantic routing hub.
- Testing and Review always return to Coordinator through Hermes.
- Hermes executes routing, verifies evidence, and owns all git/GitHub mutations.
- Agents may inspect git/PR state but never mutate repository or remote state.

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

This workflow is sequential. Do not add parallel execution, generic adapters, databases, webhooks, or auto-merge behavior.

## Preconditions

1. The target repository is local, on a dedicated feature branch, and clean.
2. `codex` is installed/authenticated.
3. `gh` is installed/authenticated for the target repository.
4. `run_codex.py` and `prompts/` are siblings of this `SKILL.md`.
5. Choose a stable workflow id, normally `issue-<number>` or `pr-<number>`.

## Git Ownership Invariant

Hermes owns all repository-state mutation:

- branch creation/switching;
- `git add`, commit, push, restore/reset/clean/rebase/merge when required;
- PR creation and metadata updates;
- PR handoff comments;
- marking a draft PR ready;
- final merge after explicit user approval.

Codex agents may use read-only commands such as `git status`, `git diff`, `git log`, `git show`, `git rev-parse`, and read-only `gh` queries. They must not mutate `.git/` or remote state, including via `gh api`/GitHub Git Data API workarounds.

Before every Codex invocation, the worktree must be clean. `run_codex.py` enforces this.

A mutable agent starts from a clean committed HEAD and leaves only role-permitted unstaged file edits. Hermes validates those edits and either commits them or discards them before another Codex invocation.

If an agent returns `BLOCKED`, an invalid result, or fails mechanical verification after leaving edits, Hermes must not complete the agent's work on its behalf. Record the evidence, discard only the edits created since the clean pre-invocation state, restore a clean HEAD, and return the failure to Coordinator.

Agents must not include `commit` in `HERMES_RESULT`; a commit SHA becomes authoritative only after Hermes creates/verifies it.

## State Machine

```text
Coordinator
  |-- HANDOFF -> Testing -> Coordinator
  |-- HANDOFF -> Review  -> Coordinator
  |-- AWAIT_USER_DECISION -> User -> Coordinator
  |-- AWAIT_USER_MERGE -> User merge decision
  +-- BLOCKED -> stop/report
```

Hermes may reject an invalid/unverified transition, but must never substitute a different specialist. Return evidence to Coordinator and let Coordinator decide.

## Procedure

### 1. Enter through Coordinator

Hermes invokes/resumes Coordinator first with the user request, acceptance criteria, repository/PR state, and workflow evidence:

```bash
python3 <skill-dir>/run_codex.py \
  --agent coordinator \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<request + acceptance criteria + current evidence>'
```

A normal code change should first route to Testing. Material ambiguity may route to `AWAIT_USER_DECISION`.

### 2. Coordinator -> Testing

For:

```json
{"status":"HANDOFF","next_agent":"testing","task":"...","reason":"..."}
```

Hermes:

1. verifies the result contract and clean worktree;
2. if a PR already exists, publishes the descriptive Coordinator -> Testing handoff comment **before dispatch**;
3. invokes Testing with the exact Coordinator task plus necessary context.

Testing may modify tests, fixtures, and test-only helpers only. It leaves edits unstaged.

A completed RED result must include the targeted command:

```json
{"status":"RED_COMPLETE","test_command":"...","summary":"..."}
```

Hermes then verifies:

- only permitted test/test-fixture/test-helper files changed;
- the targeted command fails;
- failure is caused by the missing/incorrect required behavior, not broken test setup or unrelated failure.

If verification passes, Hermes creates the RED commit from the validated test paths, pushes it, and records the resulting SHA.

If no PR exists, Hermes opens a draft PR after the first valid RED commit. Because the initial Coordinator handoff happened before a PR existed, Hermes then publishes the retained initial Coordinator -> Testing decision followed by the Testing -> Coordinator RED handoff so the audit trail remains complete.

Then resume Coordinator with Testing's result, RED commit SHA, verification evidence, and current PR/HEAD state.

`BLOCKED` means Testing could not safely complete its test responsibility. Git sandbox inability is not a blocker because Testing does not own commits.

### 3. Coordinator implementation / GREEN candidate

After verified RED, Coordinator decides whether to request more Testing work, implement/fix production code, or ask the user.

When implementation is appropriate, Coordinator edits production files in the shared worktree, runs targeted/full tests, and returns a Review handoff without creating a commit:

```json
{
  "status":"HANDOFF",
  "next_agent":"review",
  "task":"...",
  "reason":"...",
  "test_command":"...",
  "full_test_command":"..."
}
```

Use `full_test_unavailable_reason` instead of `full_test_command` only when no full suite exists.

Before Review, Hermes verifies mechanically:

- changes are within the intended implementation scope;
- RED test intent was not silently weakened;
- the reported targeted test passes;
- the full suite passes, or an explicit no-suite reason is valid.

If local verification fails, discard the unverified implementation edits back to the clean pre-invocation HEAD and resume Coordinator with the failure evidence. Do not create a WIP/GREEN commit for failed verification.

If local verification passes, Hermes:

1. creates the GREEN commit from the validated implementation state;
2. pushes it;
3. waits for configured CI and requires it to pass;
4. updates the PR description to reflect actual RED/GREEN state;
5. publishes the descriptive Coordinator -> Review handoff with the resulting GREEN SHA and verification evidence;
6. invokes fresh Review.

If push/CI verification fails after commit creation, return the evidence to Coordinator at the now-clean committed HEAD; do not invoke Review.

### 4. Review -> Coordinator

Review is always a fresh Codex session and must leave the worktree unchanged.

Hermes invokes Review with current committed HEAD, acceptance criteria, RED evidence, GREEN verification, PR description, and relevant diff.

For `REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`:

1. verify Review did not modify files or choose `next_agent`;
2. publish the descriptive Review -> Coordinator handoff;
3. if Review is clean, update the PR description to reflect completed Review and mark a draft PR ready (`draft=false`) before resuming Coordinator;
4. resume Coordinator with Review result, reviewed HEAD, PR state, and evidence.

`CHANGES_REQUIRED` returns to Coordinator. Coordinator may fix implementation, route a test gap to Testing, ask the user, or request another fresh Review after a newly verified GREEN commit.

### 5. User decision

`AWAIT_USER_DECISION` requires a non-empty `question`. Hermes asks it and stops specialist execution. The user's answer resumes the same Coordinator session.

### 6. Merge Gate

Coordinator may return merge readiness only for an explicitly non-draft PR:

```json
{"status":"AWAIT_USER_MERGE","reviewed_head":"<sha>","draft":false,"summary":"..."}
```

Before asking the user to merge, Hermes mechanically verifies:

- Review returned `REVIEW_CLEAN` for `reviewed_head`;
- current HEAD equals `reviewed_head`;
- worktree is clean;
- required targeted/full tests and configured CI pass;
- the PR description reflects actual implementation/test/Review status;
- GitHub reports `draft=false`.

If any check fails, return the evidence to Coordinator. **Never present a Draft PR as merge-ready.**

If all checks pass, publish the Coordinator -> User merge-gate comment and ask the user whether to merge. Never merge without explicit approval.

## Hermes Routing Rules

Hermes must:

- execute only valid Coordinator handoffs to `testing` or `review`;
- require a concrete `task` for every handoff;
- require Testing `RED_COMPLETE` to include `test_command`;
- require Review handoff to include `test_command` and exactly one of `full_test_command` / `full_test_unavailable_reason`;
- reject any agent result containing `commit`;
- reject Testing/Review results containing `next_agent`;
- require `question` for `AWAIT_USER_DECISION`;
- require `reviewed_head` and `draft=false` for `AWAIT_USER_MERGE`;
- keep Coordinator and Testing sessions persistent per workflow and Review fresh;
- preserve a clean worktree boundary between Codex invocations;
- create/push verified RED and GREEN commits itself;
- own PR metadata/comments and verify draft state;
- return every specialist result and verification failure to Coordinator.

Hermes must not infer `Testing -> Review`, `Review -> Testing`, or `Review -> User` transitions.

## PR Handoff Comments

Hermes owns the human-readable audit trail. Comments should explain **why the transition happened and exactly what the next agent is being asked to do**, not merely name the destination.

Once a PR exists, publish a new top-level comment for every verified handoff. Coordinator specialist handoffs are published before specialist dispatch; the initial pre-PR handoff is backfilled immediately after the first RED PR is created.

### Coordinator -> Testing

```text
### Coordinator -> Testing handoff
Status: HANDOFF
HEAD: <current-head>
Decision: <why Testing is needed>
Task: <specific behavior/test gap to address>
Evidence: <acceptance criterion, Review finding, live evidence, or prior RED gap>
Next: Testing
```

### Testing -> Coordinator

```text
### Testing -> Coordinator handoff
Status: <RED verified | verification failed | BLOCKED>
RED commit: <Hermes-created sha or n/a>
Test command: <targeted command or n/a>
Coverage: <behaviors specified>
Verification: <why RED is valid or exact failure>
Next: Coordinator
```

### Coordinator -> Review

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

### Review -> Coordinator

```text
### Review -> Coordinator handoff
Status: <REVIEW_CLEAN | CHANGES_REQUIRED | BLOCKED>
Reviewed HEAD: <sha>
Scope: <what was reviewed>
Findings: <confirmed findings or none>
Verification: <fresh-session / PR / CI evidence>
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

Do not publish success claims when mechanical verification failed. Publish actual failure evidence when useful, then return control to Coordinator.

## Agent Session Policy

- Coordinator: persistent Codex session per workflow id.
- Testing: persistent Codex session per workflow id.
- Review: fresh Codex session every invocation; never persisted.
- Role name is the agent identity; Codex thread id is session state only.

Runtime session state lives under `<skill-dir>/state/<workflow-id>.json` unless `--state-file` is supplied.

## Result Contract

Every agent finishes with exactly one `HERMES_RESULT={...}` line.

Expected statuses:

- Coordinator: `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED`.
- Testing: `RED_COMPLETE`, `BLOCKED`.
- Review: `REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED`.

No agent may include `commit`. Testing/Review may not include `next_agent`.

Treat missing, malformed, unexpected, role-incompatible, or contradictory results as failures. Never infer success/routing from prose.

## Pitfalls

- Do not let Hermes choose the semantic destination.
- Do not let agents mutate git or GitHub state, including API-based remote commits.
- Do not start any Codex invocation from a dirty worktree.
- Do not let Hermes rescue a `BLOCKED` agent by completing its domain work.
- Do not let Testing modify production code to validate its tests.
- Do not let Coordinator weaken RED test intent.
- Do not let Review modify files or reuse a previous Review session.
- Do not request Review before Hermes has created/pushed a locally verified GREEN commit and CI is green.
- Do not leave PR metadata stale after GREEN or Review.
- Do not present `draft=true` as merge-ready.
- Do not auto-merge.

## Verification

For this skill repository:

```bash
python3 -m unittest tests/test_run_codex.py -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py
```

For an end-to-end smoke test, confirm that repeated Testing/Coordinator and Review/Coordinator loops preserve clean-worktree boundaries, Hermes-created commits are the visible HEAD, every Coordinator routing decision is auditable in the PR, Review sessions are fresh, and the merge gate cannot pass while the PR is Draft.
