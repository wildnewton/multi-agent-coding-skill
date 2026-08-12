---
name: multi-agent-coding
description: Orchestrate a tests-first coding workflow with Coordinator as the semantic routing hub and Hermes as the mechanical dispatcher/verifier.
version: 0.4.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

Use this skill when the user asks Hermes to implement a code change with the multi-agent coding workflow.

## Roles and flow

- **Coordinator:** owns requirement/scope, implementation/GREEN, finding triage, semantic routing, and merge-readiness judgment.
- **Testing:** owns RED intent and test quality.
- **Review:** independently certifies the latest committed HEAD.
- **Hermes:** owns dispatch, deterministic verification, git/GitHub mutation, PR audit trail, and final user-approved merge.

Coordinator, Testing, and Review are separate Codex CLI sessions. Coordinator and Testing persist per workflow; every Review is fresh.

All transport goes through Hermes, but only Coordinator chooses semantic routing:

```text
User <-> Coordinator
          <-> Testing
          <-> Review
```

Testing and Review always return to Coordinator and never choose the next agent. The MVP is sequential: no parallel agents, generic adapters, external state service, or auto-merge.

## Preconditions

1. Target repository is available locally with a clean worktree.
2. `codex` is installed/authenticated.
3. `gh` is installed/authenticated for PR operations; merge permission is needed only for a user-approved final merge.
4. `run_codex.py` and `prompts/` are siblings of this file.
5. Choose a stable workflow id, normally `issue-<number>` or `pr-<number>`.

## Global invariants

- Hermes owns all git/GitHub mutation: branch/commit/push, restore/reset/clean/rebase/merge, PR creation/metadata/comments, Draft→Ready, and final merge. Agents may inspect git/GitHub read-only but must not mutate local or remote repository state, including through GitHub APIs. Git/GitHub mutation is not agent work, so inability to perform it is not a valid `BLOCKED` reason.
- Every Codex invocation starts from a clean worktree. Coordinator/Testing may leave only role-permitted unstaged edits; Review must leave the worktree unchanged.
- Hermes verifies permitted edits before committing them. On specialist `BLOCKED`, invalid results, or verification/git/CI failure, restore a clean state when needed and return the evidence to Coordinator; Hermes never finishes agent domain work or chooses a replacement route. If Coordinator itself is `BLOCKED`, invalid, or cannot run, stop and report the failure to the user.
- Testing owns RED intent; Coordinator routes test corrections back rather than rewriting or weakening RED tests.
- RED is for executable behavior. Do not manufacture automated contract tests for prompt/SKILL/docs/config-only changes; review them directly and validate through real execution when applicable.
- Review owns fresh-eyes certification; Coordinator never self-certifies.
- `REVIEW_CLEAN` certifies code review only. Required external/manual gates may remain open and still block merge readiness.
- Never merge without explicit user approval.

## Procedure

### 1. Start with Coordinator

Before any code edit, Hermes creates/switches to the dedicated feature branch. Invoke every role through the same runner:

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<role-specific task + current evidence>'
```

Start with Coordinator using the user request, acceptance criteria, repository/PR state, and relevant workflow evidence.

For executable behavior changes, Coordinator pins requirement/scope/gates/missing evidence and normally routes first to Testing. Prompt/SKILL/docs/config-only changes do not require manufactured RED. If safe work needs a real user decision or external/manual action, Coordinator may return `AWAIT_USER_DECISION` instead.

### 2. Coordinator -> Testing -> Coordinator

A Testing handoff requires `next_agent=testing`, non-empty `task`, and non-empty `reason`.

Testing may edit only tests, fixtures, and test helpers. On `RED_COMPLETE`, Hermes verifies:

- changed files stay within the Testing boundary;
- `test_command` runs the complete current RED set;
- RED fails for the intended missing behavior, not broken setup or unrelated failure.

If valid, Hermes creates/pushes the RED commit and opens a draft PR after the first valid RED when none exists. Resume Coordinator with the Testing result, RED SHA, verification evidence, and current HEAD/PR state.

On `BLOCKED` or failed verification, restore the clean pre-invocation HEAD and resume Coordinator with the failure evidence.

### 3. GREEN -> Review -> Coordinator

Coordinator implements the smallest GREEN and leaves edits unstaged. A Review handoff requires non-empty `task`/`reason` plus exactly one of `full_test_command` or `full_test_unavailable_reason`. If this workflow has verified RED, the targeted command comes from Testing's latest verified result.

Before Review, Hermes verifies:

- implementation scope is valid and Coordinator did not stage changes;
- when RED exists, its intent was not weakened and the verified targeted command passes;
- the full suite passes, or the supplied absence reason is valid.

On failure, discard unverified edits and resume Coordinator with evidence. On success with file edits, Hermes creates/pushes the GREEN commit and opens a draft PR if none exists. For an unchanged-HEAD re-review (for example, a PR-description-only correction), reuse the current commit and apply the Coordinator-specified PR metadata change instead of creating an empty commit.

Then Hermes checks configured CI, updates the PR description as needed, and invokes a fresh Review with current HEAD, pinned requirement/AC/scope, relevant RED evidence when applicable, PR description/diff, and relevant prior findings.

For every valid Review result, resume Coordinator with the reviewed HEAD and findings/gates. `REVIEW_CLEAN` updates review metadata but does **not** automatically mark a Draft PR ready.

Review semantics:
- `CHANGES_REQUIRED`: confirmed blocking defect or required code-review acceptance criterion cannot be validated.
- `REVIEW_CLEAN`: no confirmed blocking code/test/required-PR-description defect; non-blocking notes or external/manual gates may remain.

Coordinator then chooses the smallest justified next action: Testing, direct implementation when behavior is already pinned, fresh Review, user decision/action, or no mandatory work.

### 4. User decision

`AWAIT_USER_DECISION` must contain a specific non-empty `question`. Hermes asks it and stops specialist execution. Resume the same Coordinator session with the user's response/evidence.

### 5. Merge gate

Only Coordinator may return `AWAIT_USER_MERGE`, and only when no required external/manual gate remains unresolved. The result must include `reviewed_head` and `draft=false`.

Hermes first verifies:

- `REVIEW_CLEAN` covers `reviewed_head` and current HEAD still matches;
- worktree is clean;
- applicable targeted/full tests and configured CI pass;
- PR description matches implementation, evidence, Review status, and completed required gates.

Only after those checks pass, Hermes marks a Draft PR ready if needed and verifies GitHub reports `draft=false`. Then ask the user whether to merge. On explicit approval, Hermes performs the squash merge and closes linked issues when required by the task.

## Result contract

Every agent ends with exactly one `HERMES_RESULT={...}` line. Do not infer success/routing from prose.

- **Coordinator:** `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED`.
  - `HANDOFF`: `next_agent` (`testing|review`), non-empty `task`, non-empty `reason`; Review additionally requires exactly one full-suite field.
  - `AWAIT_USER_DECISION`: non-empty `question`.
  - `AWAIT_USER_MERGE`: non-empty `reviewed_head`, `draft=false`.
- **Testing:** `RED_COMPLETE` with non-empty `test_command`, or `BLOCKED`.
- **Review:** `REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`. `CHANGES_REQUIRED` requires at least one blocking finding; `REVIEW_CLEAN` contains none and uses `APPROVE` or `APPROVE_WITH_MINOR_NOTES`. Findings follow the Review prompt schema.
- No agent may include `commit`. Testing/Review must not include `next_agent`.

Treat malformed, contradictory, or role-incompatible specialist results as failures and return the evidence to Coordinator. Coordinator-result failures stop the workflow and are reported to the user.

## PR handoff audit trail

Hermes—not agents—publishes one new top-level PR comment for each verified handoff. Coordinator specialist handoffs are published before dispatch; the initial pre-PR Coordinator -> Testing handoff is backfilled after the first valid RED opens the draft PR.

Comments should be concise but sufficient to reconstruct the decision:

| Transition | Include |
|---|---|
| Coordinator -> Testing | HEAD, decision/reason, exact test task, decisive evidence |
| Testing -> Coordinator | RED SHA, test command, coverage, RED verification |
| Coordinator -> Review | GREEN/current SHA, decision, review scope, applicable targeted/full/CI evidence, relevant AC/RED evidence |
| Review -> Coordinator | reviewed HEAD, verdict, classified findings, external gates |
| Coordinator -> User | reviewed HEAD, `draft=false`, readiness reason, Review/tests/CI/worktree/PR/gate evidence |

Do not replay full workflow history or publish a successful verification claim when mechanical verification failed.

## Verification

For this skill repository:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py
```

For an end-to-end smoke test, use a small real issue and confirm: Coordinator can loop through Testing/Review without user nudges; each verified handoff leaves an audit comment; every Codex invocation starts clean; Hermes-created commits are visible HEAD; Review sessions are fresh; code-clean Review can coexist with an unresolved external gate; and merge cannot proceed while a required gate remains open or the PR remains Draft.
