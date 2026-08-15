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
- **Review:** independently reviews the full PR diff at the latest committed HEAD.
- **Hermes:** dispatches agents, verifies mechanical workflow evidence, and owns git/GitHub mechanics, PR audit trail, and final user-approved merge.

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
- Codex role invocations are bounded background jobs. Hermes dispatches `run_codex.py` with `background=true` and `notify_on_complete=true`; the immediate background start result is dispatch evidence only, never an agent result. Keep the workflow sequential with at most one role invocation in flight, and do not route until that process has completed and its wrapper result has been retrieved.
- A Coordinator specialist handoff remains unresolved until the specialist completes and Hermes accepts the required mechanical evidence. On timeout, `BLOCKED`, invalid output, or failed verification, Hermes investigates the mechanical failure and returns that evidence to Coordinator; Hermes does not perform specialist work or choose the semantic recovery route. Coordinator recovery while a specialist remains unresolved is read-only. `run_codex.py` owns the detailed handoff-state enforcement.
- If `run_codex.py` returns `ERROR`, treat any reported `unverified_artifacts` as failed-invocation leftovers: do not commit or reinterpret them as agent output. Hermes never finishes agent domain work or chooses a replacement semantic route. If Coordinator itself is `BLOCKED`, invalid, or cannot run, stop and report the failure to the user.
- Testing owns RED intent; Coordinator routes test corrections back rather than rewriting or weakening RED tests.
- RED is for executable behavior. Do not manufacture automated contract tests for prompt/SKILL/docs/config-only changes; review them directly and validate through real execution when applicable.
- Review owns fresh-eyes certification; Coordinator never self-certifies. `run_codex.py` records the current clean Review certification used by the merge gate.
- `REVIEW_CLEAN` certifies code review only. Required external/manual gates may remain open and still block merge readiness.
- Never merge without explicit user approval.

## Procedure

### 1. Start with Coordinator

Before any repository edit, Hermes creates/switches to the dedicated feature branch. Invoke every role through the same runner:

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<role-specific task + current evidence>' \
  --timeout-seconds 1800
```

Launch that command with Hermes terminal `background=true` and `notify_on_complete=true`. Record the returned background `session_id`, but do not treat the immediate spawn `exit_code=0` as role completion. Wait for the completion notification, then retrieve the completed process result/output before validating `HERMES_RESULT` or dispatching another role. Do not change Hermes global terminal timeout or `TERMINAL_MAX_FOREGROUND_TIMEOUT` for this workflow.

After Hermes accepts a specialist handoff, resume Coordinator with `--completed-agent <testing|review>`. Omit the flag for unresolved/failed handoffs; `run_codex.py` validates the detailed state contract.

Start with Coordinator using the user request, acceptance criteria, repository/PR state, and relevant workflow evidence.

For executable behavior changes, Coordinator pins requirement/scope/gates/missing evidence and normally routes first to Testing. Prompt/SKILL/docs/config-only changes do not require manufactured RED. If safe work needs a real user decision or external/manual action, Coordinator may return `AWAIT_USER_DECISION` instead.

### 2. Coordinator -> Testing -> Coordinator

A Testing handoff requires `next_agent=testing`, non-empty `task`, and non-empty `reason`.

Testing may edit only tests, fixtures, and test helpers. `RED_COMPLETE` is Testing's semantic certification that the reported `test_command` covers the complete current RED set and that the observed failure is for the intended missing behavior.

Hermes verifies only the mechanical workflow evidence:

- changed files stay within the Testing boundary;
- the reported `test_command` executes and exits non-zero;
- the actual command output is captured for Coordinator.

If mechanical verification passes, Hermes creates/pushes the RED commit and opens a draft PR after the first RED when none exists. Resume Coordinator with `--completed-agent testing`, the Testing result, RED SHA, actual test output, and current HEAD/PR state. Coordinator decides whether the RED evidence is semantically sufficient or should return to Testing.

On `BLOCKED` or failed invocation/verification, Hermes investigates/restores as needed and resumes Coordinator without the completion flag, including the decisive failure evidence.

### 3. GREEN -> Review -> Coordinator

Coordinator implements the smallest GREEN and leaves edits unstaged. A Review handoff requires non-empty `task`/`reason` plus exactly one of `full_test_command` or `full_test_unavailable_reason`. If this workflow has verified RED, the targeted command comes from Testing's latest verified result.

Before Review, Hermes verifies:

- Coordinator did not stage changes;
- when RED exists, the verified targeted command passes;
- the full suite passes, or a non-empty `full_test_unavailable_reason` was supplied.

On failure, discard unverified edits and resume Coordinator with evidence. On success with file edits, Hermes creates/pushes the GREEN commit and opens a draft PR if none exists. For an unchanged-HEAD re-review (for example, a PR-description-only correction), reuse the current commit and apply the Coordinator-specified PR metadata change instead of creating an empty commit.

Then Hermes checks configured CI and updates the PR description from Coordinator-supplied semantic content. Invoke a fresh Review with the issue/request reference, current HEAD, pinned requirement/AC/scope, relevant RED evidence when applicable, PR description/diff, relevant prior findings, and mechanical production/test diff stats when available.

`REVIEW_CLEAN` or `CHANGES_REQUIRED` completes Review work; after either accepted result, resume Coordinator with `--completed-agent review`. On Review failure/`BLOCKED`, investigate and resume Coordinator without the completion flag. `REVIEW_CLEAN` does **not** automatically mark a Draft PR ready.

Coordinator then chooses the smallest justified next action: Testing, direct implementation when behavior is already pinned, fresh Review, user decision/action, or no mandatory work.

### 4. User decision

`AWAIT_USER_DECISION` must contain a specific non-empty `question`. Hermes asks it and stops specialist execution. Resume the same Coordinator session with the user's response/evidence.

### 5. Merge gate

Only Coordinator may return `AWAIT_USER_MERGE`, and only when no required external/manual gate remains unresolved. The result must include `reviewed_head` and `draft=false`.

`run_codex.py` rejects merge readiness when a specialist handoff is unresolved or current HEAD lacks clean Review certification.

Hermes then verifies the remaining mechanical/external gates:

- the actual GitHub PR HEAD equals `reviewed_head`;
- worktree is clean;
- applicable targeted/full tests and configured CI pass;
- the PR description has not changed since that `REVIEW_CLEAN`;
- no required external/manual gate remains unresolved.

Only after those checks pass, Hermes marks a Draft PR ready if needed and verifies GitHub reports `draft=false`. Then ask the user whether to merge. On explicit approval, Hermes performs the squash merge with `reviewed_head` as the atomic expected-head precondition. If the PR HEAD has moved, do not merge; resume Coordinator with the current PR HEAD and mismatch evidence. Close linked issues only after a successful merge.

## Result contract

Every agent ends with exactly one `HERMES_RESULT={...}` line. Do not infer success/routing from prose.

- **Coordinator:** `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED`.
  - `HANDOFF`: `next_agent` (`testing|review`), non-empty `task`, non-empty `reason`; Review additionally requires exactly one full-suite field.
  - `AWAIT_USER_DECISION`: non-empty `question`.
  - `AWAIT_USER_MERGE`: non-empty `reviewed_head`, `draft=false`.
- **Testing:** `RED_COMPLETE` with non-empty `test_command`, or `BLOCKED`.
- **Review:** `REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`; verdict and finding semantics follow the Review prompt.
- No agent may include `commit`. Testing/Review must not include `next_agent`.

Treat malformed, contradictory, or role-incompatible specialist results as failures and return the evidence to Coordinator. Coordinator-result failures stop the workflow and are reported to the user. A successful background process spawn is not a result contract; only the completed `run_codex.py` process can produce a valid agent result.

## PR handoff audit trail

Hermes—not agents—publishes one new top-level PR comment for each verified handoff. Coordinator specialist handoffs are published before dispatch; the initial pre-PR Coordinator -> Testing handoff is backfilled after the first valid RED opens the draft PR.

Use the heading `### <From> -> <To> handoff`; include the relevant HEAD/checkpoint in the body so the transition is mechanically attributable to a commit.

Comments should be concise but sufficient to reconstruct the decision:

| Transition | Include |
|---|---|
| Coordinator -> Testing | HEAD, decision/reason, exact test task, decisive evidence |
| Testing -> Coordinator | RED SHA, test command, coverage, RED verification |
| Coordinator -> Review | GREEN/current SHA, decision, review scope, applicable targeted/full/CI evidence, relevant AC/RED evidence |
| Review -> Coordinator | reviewed HEAD, verdict, classified findings, external gates, production/test diff stats when available |
| Coordinator -> User | reviewed HEAD, `draft=false`, readiness reason, Review/tests/CI/worktree/PR/gate evidence |

Do not replay full workflow history or publish a successful verification claim when mechanical verification failed.

## Verification

For this skill repository:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

For an end-to-end smoke test, use a small real issue and confirm: Coordinator can loop through Testing/Review without user nudges; each verified handoff leaves an audit comment; every Codex invocation starts clean; Hermes-created commits are visible HEAD; Review sessions are fresh; code-clean Review can coexist with an unresolved external gate; and merge cannot proceed while a required gate remains open or the PR remains Draft.
