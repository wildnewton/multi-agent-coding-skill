---
name: multi-agent-coding
description: Orchestrate a task-reviewed, tests-first coding workflow with Coordinator as semantic routing authority and run_codex.py as the deterministic handoff executor.
version: 0.4.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

Use this skill when the user asks Hermes to implement a code change with the multi-agent coding workflow.

## Roles

- **User:** owns product/domain decisions and destructive authorization, including final merge approval.
- **Coordinator:** owns the canonical task, requirement/scope, implementation/GREEN, finding triage, semantic routing, and merge-readiness judgment.
- **Task Review:** independently certifies the task before implementation.
- **Testing:** owns RED intent and test quality.
- **Review:** independently certifies the implemented PR diff at the latest committed HEAD.
- **Executor (`run_codex.py`):** owns deterministic handoff/state/audit mechanics and mechanical gate enforcement.
- **Hermes:** transports user intent/answers and performs remaining branch/commit/push/CI/PR/approved-merge mechanics outside the Executor.

Only Coordinator chooses semantic routing. Coordinator and Testing persist per workflow; every Task Review and Review is fresh.

## Core operating rules

- Agents never mutate git or GitHub state. Task Review and Review are read-only; Coordinator is read-only until Task Review is clean.
- Before the first agent invocation that may edit the repository, Hermes must ensure the workflow is on a dedicated feature branch.
- New semantic code-change work must pass fresh Task Review before Testing, GREEN, or Review.
- Workflow transport is one durable outstanding handoff: `pending = { from, to, payload }`. For agent-to-agent work, `pending` means the From Agent's handoff has been accepted but the To Agent has not consumed it yet.
- Agent-to-agent transitions use **ACCEPT -> BRIDGE -> DISPATCH**: Executor accepts/persists the From Agent result, Hermes performs required git/GitHub mechanics, then Executor verifies the pending receiver, publishes the handoff trace from the actual dispatch state, and invokes the To Agent from the exact pending payload.
- Testing `RED_COMPLETE` is mechanically accepted only while its reported `test_command` still fails; the verification is timeout-bounded and must not change repository state beyond the Testing edits already present.
- Specialist timeout, `BLOCKED`, malformed/invalid output, non-zero exit, or failed mechanical acceptance leaves the original specialist handoff unresolved. Recovery Coordinator remains read-only and, in this MVP, may only replace that ownership with a new specialist `HANDOFF` or return `BLOCKED`; it cannot wait on a user decision while specialist ownership is unresolved.
- Task Review clean certification persists as the accepted task checkpoint. Review clean certification persists as reviewed HEAD + PR-description identity. Stale certification blocks merge readiness.
- Task Review handoffs stay on the canonical Issue. Other formal agent traces are published at dispatch: before a PR exists they use the Issue; once a PR exists they use the PR. Do not backfill earlier handoffs.
- Audit is fail-closed but not exactly-once; do not add handoff IDs/history/dedup machinery for rare duplicate comments.
- Never merge without explicit user approval.

`run_codex.py` and runtime tests are authoritative for detailed transition/state validation. Agent result schemas and semantic review criteria live in the role prompts; do not duplicate them here.

## Invocation

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|task_review|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<initial user task, recovery evidence, or user answer>' \
  --timeout-seconds 1800
```

Use a stable workflow id, normally `issue-<number>` or `pr-<number>`; use `issue-<number>` when Task Review must trace to a canonical Issue.

Run long-lived role invocations as background jobs with completion notification. Process spawn is dispatch evidence only; do not route again until `run_codex.py` has completed and its result has been retrieved.

Hermes may specify which role to invoke, but the Executor rejects a role that is not the current legal receiver. Specialist content comes from `pending.payload`; `--task` is only the initial Coordinator task, recovery evidence, or a user answer when waiting on the user.

## Agent handoff cycle

Every formal agent-to-agent transition follows the same three steps:

1. **ACCEPT — From Agent -> Executor**  
   Executor validates the result contract and required role-owned mechanical evidence, then persists the exact `pending {from,to,payload}`. Acceptance does not dispatch the receiver.
2. **BRIDGE — Executor -> Hermes**  
   Control returns to Hermes. Hermes performs only the git/GitHub mechanics needed before the next role: commit/push, targeted/full tests, CI, Draft PR creation/update, or PR metadata. If none are needed, this is a no-op.
3. **DISPATCH — Hermes -> Executor -> To Agent**  
   Hermes invokes the pending receiver. Executor re-checks the legal receiver and clean dispatch state, and once implementation is in PR phase also requires the PR to exist with actual PR HEAD equal to local HEAD. It then publishes the formal handoff trace using the actual dispatch HEAD/location and invokes the To Agent from the exact `pending.payload`.

This cycle uses the existing single `pending`; do not add a phase flag, bridge-complete flag, handoff ID, or second pending object.

After Task Review is clean, do **not** create an empty commit merely to open a PR. As soon as the first real implementation-stage commit exists—normally RED, otherwise GREEN—Hermes pushes it and opens the Draft PR before the next agent dispatch. Therefore Task Review history remains on the Issue; the first `Coordinator -> Testing` dispatch may still be on the Issue when no implementation commit exists yet, while `Testing -> Coordinator` and subsequent implementation-stage traces go to the PR. Do not backfill that first dispatch.

## Workflow

1. **Coordinator -> Task Review -> Coordinator**  
   Start from the user request and decisive repository evidence. Executor accepts Coordinator's Task Review handoff, then dispatches fresh Task Review from the exact pending payload. Task Review results are accepted back into `Task Review -> Coordinator` with the Executor-computed reviewed-task checkpoint and dispatched to Coordinator. Repeat `CHANGES_REQUIRED` iterations until clean. No implementation/test edits are allowed before clean Task Review.

2. **Coordinator -> Testing -> Coordinator**  
   For executable behavior needing new/corrected test intent, Executor accepts Coordinator's Testing handoff and dispatches Testing. Testing owns test/fixture/helper edits; Executor mechanically verifies RED before accepting `Testing -> Coordinator`. Hermes then commits/pushes RED and, if this is the first real implementation commit, opens the Draft PR before Executor dispatches Coordinator. The initial `Coordinator -> Testing` trace may therefore be on the Issue; `Testing -> Coordinator` and later implementation traces are on the PR.

3. **GREEN -> Review -> Coordinator**  
   Coordinator implements the smallest GREEN and returns a Review handoff. Executor accepts it; Hermes commits/pushes GREEN, runs applicable tests/CI, and updates the PR description. Executor then publishes `Coordinator -> Review` at the final dispatch HEAD and invokes fresh Review. `REVIEW_CLEAN` certifies that reviewed HEAD + PR description; Review fixes require the same ACCEPT -> BRIDGE -> DISPATCH loop again.

4. **User decision**  
   On `AWAIT_USER_DECISION`, Hermes asks the user and passes the exact answer back. The Executor persists `User -> Coordinator` before resuming Coordinator so retry reuses the accepted answer. This path is only available when no specialist handoff remains unresolved.

5. **Merge approval**  
   After `REVIEW_CLEAN` is accepted and before Executor dispatches `Review -> Coordinator`, Hermes finishes remaining tests/CI/external gates, marks the Draft PR ready if appropriate, and verifies GitHub reports `draft=false`. Coordinator then makes the final semantic merge-readiness judgment. On `AWAIT_USER_MERGE`, Executor mechanically re-checks a clean worktree, valid Task Review/Review certifications, no unresolved specialist ownership, current local/PR HEAD consistency, unchanged reviewed PR description, and actual GitHub `draft=false` before creating `Coordinator -> User`. On explicit approval, Hermes merges with `reviewed_head` as the atomic expected-HEAD precondition; if the PR HEAD moved, do not merge and return the current PR HEAD plus mismatch evidence to Coordinator.

Capability-isolating raw git/GitHub privileges behind the Executor is outside this MVP.

## Failure handling

- `ERROR` is not an agent result; never reinterpret partial output as accepted work.
- Do not commit reported `unverified_artifacts`.
- Specialist failure leaves its pending ownership unresolved. Before recovery, Hermes discards/restores failed or `BLOCKED` invocation leftovers, then gives decisive failure evidence to read-only Coordinator rather than doing specialist work itself.
- Dispatch-trace or dispatch-bridge failure leaves the accepted pending handoff in place and prevents the To Agent from running; complete/fix the bridge and retry dispatch rather than reconstructing the handoff.
- Report unrecoverable Coordinator failure to the user.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

The opt-in long-running smoke harness must be primed with a legal pending Testing handoff; see `tests/smoke_long_running_invocation.py`.
