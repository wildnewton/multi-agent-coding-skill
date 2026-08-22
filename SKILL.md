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
- **Coordinator:** owns the canonical task, requirement/scope, implementation/GREEN, finding triage, external-verification interpretation, semantic routing, and merge-readiness judgment.
- **Task Review:** independently certifies the task before implementation.
- **Testing:** owns RED intent, explicitly authorized test-only corrections, and test quality.
- **Review:** independently certifies the implemented PR diff at the latest committed HEAD and, when required, validates preserved external-verification evidence/classification.
- **Executor (`run_codex.py`):** owns deterministic handoff/state/audit mechanics, mechanical gate enforcement, and mechanical execution/recording of required external verification when its environment can exercise the boundary.
- **Hermes:** transports user intent/answers and performs remaining branch/commit/push/CI/PR/approved-merge mechanics outside the Executor.

Only Coordinator chooses semantic routing. Coordinator and Testing persist per workflow; every Task Review and Review is fresh.

## Core operating rules

- Agents never mutate git or GitHub state. Task Review and Review are read-only; Coordinator is read-only until Task Review is clean.
- Before the first agent invocation that may edit the repository, Hermes must ensure the workflow is on a dedicated feature branch.
- New semantic code-change work must pass fresh Task Review before Testing, GREEN, or Review.
- Verification/no-change work may terminate with Coordinator `COMPLETED`; if a previously clean task materially changes to no-change, send the revised task through fresh Task Review first.
- Workflow transport is one durable outstanding handoff: `pending = { from, to, payload }`. For agent-to-agent work, `pending` means the From Agent's handoff has been accepted but the To Agent has not consumed it yet. The same single pending also represents `Coordinator -> Executor` ownership for one required external-verification action.
- Agent-to-agent transitions use **ACCEPT -> BRIDGE -> DISPATCH**: Executor accepts/persists the From Agent result, Hermes performs required git/GitHub mechanics, then Executor verifies the pending receiver, publishes the handoff trace from the actual dispatch state, and invokes the To Agent from the exact pending payload.
- Ordinary Testing `RED_COMPLETE` is mechanically accepted only while its reported `test_command` still fails; the verification is timeout-bounded and must not change repository state beyond the Testing edits already present.
- A confirmed existing test/fixture/test-helper defect whose correct repair should pass may use the narrow `testing_intent: "test_fix"` handoff with exact repository-relative `allowed_paths`. Only that handoff may complete with `TEST_FIX_COMPLETE`; Executor requires all changed paths to stay within `allowed_paths` and the reported `test_command` to pass. This does not add a workflow phase or weaken ordinary RED.
- When the Task-Review-certified acceptance criteria require one real live/external gate, Coordinator may return `VERIFY_EXTERNAL` with one non-destructive command, boundary, and reason. Executor accepts it as `Coordinator -> Executor`; Hermes finishes the commit/push/PR-HEAD bridge; then Executor mechanically runs/records the command and reverses the same pending to `Executor -> Coordinator`. Non-zero/timeout/command-execution failure is evidence for Coordinator classification, not automatic regression. Executor/orchestration failure remains workflow `ERROR` and leaves `Coordinator -> Executor` ownership unresolved.
- If the Executor environment cannot exercise the required boundary, Coordinator may use the existing `AWAIT_USER_DECISION` required-action path with structured external-verification metadata for the exact committed HEAD. Returned evidence is preserved with `externally_supplied` provenance; it is not mechanically attested. Do not create a remote-runner/environment-provisioning framework in this MVP.
- Required external evidence is exact-HEAD and survives Coordinator consumption as one current checkpoint so fresh Review receives the same evidence separately from Coordinator's classification. If HEAD changes, the evidence is stale. Replacing required evidence invalidates any existing Review certification. This MVP supports at most one required external-verification gate per canonical task, expressed by one command/suite.
- If Coordinator introduces a materially new required external gate rather than executing one already implied by the certified task/acceptance criteria, the revised canonical task must pass fresh Task Review first.
- Specialist timeout, `BLOCKED`, malformed/invalid output, non-zero exit, or failed mechanical acceptance leaves the original specialist handoff unresolved. Recovery Coordinator remains read-only and, in this MVP, may only replace that ownership with a new specialist `HANDOFF` or return `BLOCKED`; it cannot wait on a user decision while specialist ownership is unresolved.
- Task Review clean certification persists as the accepted task checkpoint. Review clean certification persists as reviewed HEAD + PR-description identity. Stale certification blocks merge readiness.
- Task Review handoffs stay on the canonical Issue. Other formal agent traces are published at dispatch: before a PR exists they use the Issue; once a PR exists they use the PR. Do not backfill earlier handoffs.
- Audit is fail-closed but not exactly-once; do not add handoff IDs/history/dedup machinery for rare duplicate comments.
- Never merge without explicit user approval.

`run_codex.py` and runtime tests are authoritative for detailed transition/state validation. Agent result schemas and semantic review criteria live in the role prompts; do not duplicate them here.

## Invocation

Agent invocation:

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|task_review|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<initial user task, recovery evidence, or user answer>' \
  --timeout-seconds 1800
```

Pending required external verification is a mechanical Executor action, not an agent invocation:

```bash
python3 <skill-dir>/run_codex.py \
  --run-external-verification \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --timeout-seconds 1800
```

Use a stable workflow id, normally `issue-<number>` or `pr-<number>`; use `issue-<number>` when Task Review must trace to a canonical Issue.

Run long-lived role invocations as background jobs with completion notification. Process spawn is dispatch evidence only; do not route again until `run_codex.py` has completed and its result has been retrieved. Apply the same wait-for-result rule to a pending external-verification action.

Hermes may specify which agent role to invoke, but the Executor rejects a role that is not the current legal receiver. Specialist content comes from `pending.payload`; `--task` is only the initial Coordinator task, recovery evidence, or a user answer when waiting on the user. `--run-external-verification` consumes only a legal pending `Coordinator -> Executor` `VERIFY_EXTERNAL` request.

## Agent handoff cycle

Every formal agent-to-agent transition follows the same three steps:

1. **ACCEPT — From Agent -> Executor**  
   Executor validates the result contract and required role-owned mechanical evidence, then persists the exact `pending {from,to,payload}`. Acceptance does not dispatch the receiver.
2. **BRIDGE — Executor -> Hermes**  
   Control returns to Hermes. Hermes performs only the git/GitHub mechanics needed before the next role: commit/push, targeted/full tests, CI, Draft PR creation/update, or PR metadata. If none are needed, this is a no-op.
3. **DISPATCH — Hermes -> Executor -> To Agent**  
   Hermes invokes the pending receiver. Executor re-checks the legal receiver and clean dispatch state, and once implementation is in PR phase also requires the PR to exist with actual PR HEAD equal to local HEAD. It then publishes the formal handoff trace using the actual dispatch HEAD/location and invokes the To Agent from the exact `pending.payload`.

This cycle uses the existing single `pending`; do not add a phase flag, bridge-complete flag, handoff ID, or second pending object.

A `Coordinator -> Executor` required external-verification request reuses the same ACCEPT/BRIDGE ownership pattern but ends in a mechanical action instead of invoking Codex: after Hermes has committed/pushed/synchronized the candidate, `--run-external-verification` validates current HEAD/PR HEAD, publishes the accepted handoff trace, runs the command without semantic interpretation, persists the current evidence checkpoint, and reverses pending to `Executor -> Coordinator`.

After Task Review is clean, do **not** create an empty commit merely to open a PR. As soon as the first real implementation-stage commit exists—normally RED, otherwise GREEN—Hermes pushes it and opens the Draft PR before the next agent dispatch. Therefore Task Review history remains on the Issue; the first `Coordinator -> Testing` dispatch may still be on the Issue when no implementation commit exists yet, while `Testing -> Coordinator` and subsequent implementation-stage traces go to the PR. Do not backfill that first dispatch.

## Workflow

1. **Coordinator -> Task Review -> Coordinator**  
   Start from the user request and decisive repository evidence. Executor accepts Coordinator's Task Review handoff, then dispatches fresh Task Review from the exact pending payload. Task Review results are accepted back into `Task Review -> Coordinator` with the Executor-computed reviewed-task checkpoint and dispatched to Coordinator. Repeat `CHANGES_REQUIRED` iterations until clean. No implementation/test edits are allowed before clean Task Review.

2. **Coordinator -> Testing -> Coordinator**  
   For executable behavior needing new/corrected test intent, Executor accepts an ordinary Coordinator Testing handoff and dispatches Testing. Testing owns test/fixture/helper edits; Executor mechanically verifies RED before accepting `Testing -> Coordinator`. If an already-pinned/GREEN behavior instead exposes a confirmed existing test/fixture/test-helper defect whose correct repair should pass, Coordinator uses `testing_intent: "test_fix"` plus exact `allowed_paths`; Executor accepts `TEST_FIX_COMPLETE` only when the changed paths stay within that allowlist and the reported command passes. Hermes then commits/pushes accepted Testing edits and, if this is the first real implementation commit, opens the Draft PR before Executor dispatches Coordinator. The initial `Coordinator -> Testing` trace may therefore be on the Issue; `Testing -> Coordinator` and later implementation traces are on the PR.

3. **GREEN -> required external verification when applicable -> Review -> Coordinator**  
   Coordinator implements the smallest GREEN. If the certified acceptance criteria require one live/external gate, Coordinator requests it only against a committed candidate: normally return `VERIFY_EXTERNAL`, let Hermes finish the commit/push/PR-HEAD bridge, then run the pending Executor action. Coordinator consumes the resulting evidence and semantically classifies failures. If the Executor environment cannot exercise the boundary, Coordinator may instead use structured `AWAIT_USER_DECISION` once the exact candidate HEAD is already committed/current; the returned evidence is preserved as externally supplied evidence. Testing is re-entered only if classification exposes Testing-owned harness/coverage work or focused RED is required. Any fix that changes HEAD makes earlier external evidence stale and requires rerun.

   When GREEN and required external evidence are ready, Coordinator returns a Review handoff containing the semantic PR-description content, requirement/acceptance criteria, exact review scope, Coordinator's external-evidence classification/rationale when applicable, and the full-suite command or why none exists. Executor accepts it; Hermes completes remaining commit/push/test/CI/PR-description bridge mechanics that do not create new required evidence. Executor verifies required external evidence is current-head, supplies the same preserved evidence separately to fresh Review, publishes `Coordinator -> Review`, and invokes Review from the exact pending payload. `REVIEW_CLEAN` certifies that reviewed HEAD + PR description; Review fixes or replacement external evidence require fresh Review again.

4. **User decision / required action**  
   On `AWAIT_USER_DECISION`, Hermes asks the user and passes the exact answer back. The Executor persists `User -> Coordinator` before resuming Coordinator so retry reuses the accepted answer. This path is only available when no specialist or Executor action handoff remains unresolved.

   When `AWAIT_USER_DECISION` carries structured external-verification metadata, the request is for one exact non-destructive run in another capable environment because the Executor environment cannot exercise the boundary. The returned answer is still persisted as the exact User -> Coordinator answer and is additionally preserved as the current `externally_supplied` verification checkpoint. Coordinator and Review must validate its sufficiency/provenance; Hermes must not interpret it semantically.

   When an active workflow is pending on the User, Hermes must classify the reply against that pending workflow before acting. If an `AWAIT_USER_MERGE` reply explicitly approves merge, use the merge path below. If it instead requests additional investigation, testing, or modification of the same task/PR, Hermes must not perform that semantic investigation or production/test work directly; invoke Coordinator with the same workflow ID and the exact user reply so the existing `User -> Coordinator` transport resumes semantic routing under the normal Task Review / Testing / GREEN / Review rules. An unrelated user request may be handled separately, but it must not be passed as the answer to or otherwise consume or replace the existing `Coordinator -> User` pending handoff.

5. **Merge approval**  
   After `REVIEW_CLEAN` is accepted and before Executor dispatches `Review -> Coordinator`, Hermes may finish remaining ordinary test/CI mechanics and marks the Draft PR ready if appropriate, but a required live/external gate must already have current-head preserved evidence that fresh Review saw; do not first satisfy required external verification after Review. Coordinator then makes the final semantic merge-readiness judgment. On `AWAIT_USER_MERGE`, Executor mechanically re-checks a clean worktree, valid Task Review/Review certifications, no unresolved specialist/Executor ownership, current local/PR HEAD consistency, unchanged reviewed PR description, current-head preserved external evidence when one exists, and actual GitHub `draft=false` before creating `Coordinator -> User`. On explicit approval, Hermes merges with `reviewed_head` as the atomic expected-HEAD precondition; if the PR HEAD moved, do not merge and return the current PR HEAD plus mismatch evidence to Coordinator.

Capability-isolating raw git/GitHub privileges behind the Executor is outside this MVP.

## Failure handling

- `ERROR` is not an agent result; never reinterpret partial output as accepted work.
- Do not commit reported `unverified_artifacts`.
- Specialist failure leaves its pending ownership unresolved. This includes invalid RED/test-fix completion, failed test-fix verification, or test-fix edits outside `allowed_paths`. Before recovery, Hermes discards/restores failed or `BLOCKED` invocation leftovers, then gives decisive failure evidence to read-only Coordinator rather than doing specialist work itself.
- Required external-verification command outcomes (`non-zero`, timeout, or command execution failure) are captured as evidence and returned `Executor -> Coordinator`; they are not Executor failures. By contrast, invalid pending/state, failed bridge/HEAD/audit validation, repository mutation, state persistence failure, or another Executor/orchestration failure leaves `Coordinator -> Executor` pending unresolved. Fix/retry the mechanical action; do not reinterpret that workflow failure as environment/external-service evidence.
- Dispatch-trace or dispatch-bridge failure leaves the accepted pending handoff in place and prevents the To Agent/action from running; complete/fix the bridge and retry dispatch rather than reconstructing the handoff.
- Report unrecoverable Coordinator failure to the user.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

The opt-in long-running smoke harness must be primed with a legal pending Testing handoff; see `tests/smoke_long_running_invocation.py`.
