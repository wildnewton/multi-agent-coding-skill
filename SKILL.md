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

## Ownership

- **User:** product/domain decisions, destructive authorization, and final merge approval.
- **Coordinator:** canonical task, requirement/scope, implementation/GREEN, finding triage, semantic routing, and merge-readiness judgment.
- **Task Review:** fresh independent pre-implementation task certification.
- **Testing:** RED intent, explicitly authorized test-only corrections, and test quality.
- **Review:** fresh independent Review with an explicit scope: full code/test/PR review before required external verification, or evidence-only certification after it.
- **Executor (`run_codex.py`):** deterministic handoff/state/audit mechanics and mechanical gates.
- **Hermes:** user transport plus branch/commit/push/test/CI/PR/approved-merge mechanics, including host-side required external verification through the Executor.

Only Coordinator chooses semantic routing. Coordinator and Testing persist per workflow; every Task Review and Review is fresh. `run_codex.py` and runtime tests are authoritative for mechanical transition/state validation; role prompts own role-local semantic contracts.

## Operator flow

### 1. Start or resume

Use one stable workflow id, normally `issue-<number>` or `pr-<number>`; use `issue-<number>` when Task Review must trace to a canonical Issue.

Before the first agent invocation that may edit the repository, ensure a dedicated feature branch exists. New semantic code-change work must pass fresh Task Review before Testing, GREEN, or Review. Verification/no-change work may end with Coordinator `COMPLETED`; if a previously clean task materially changes to no-change, send the revised task through fresh Task Review first.

Invoke only the current legal receiver. Specialist work comes from the Executor's pending payload. `--task` carries fresh semantic input only: the initial Coordinator task, recovery evidence, or an accepted user answer.

### 2. Bridge and dispatch

The workflow has one durable outstanding handoff: `pending = { from, to, payload }`. For an agent-to-agent transition, Executor accepts the From Agent result into `pending`, then control returns to Hermes. Hermes performs only the required bridge mechanics—such as commit/push, targeted/full tests, CI, Draft PR creation/update, or PR metadata—then invokes the pending receiver. Executor validates the dispatch state, publishes the handoff trace, and calls that receiver from the exact pending payload.

Do not route again until the current `run_codex.py` invocation has completed and its result has been retrieved. The same rule applies to pending external-verification actions.

Commit/push only work already accepted by the Executor. After clean Task Review, do not create an empty commit just to open a PR. Open the Draft PR after the first real implementation-stage commit (normally RED, otherwise GREEN) and before the next agent dispatch. Task Review traces stay on the Issue; other traces use the Issue until a PR exists and the PR thereafter. Do not backfill earlier traces.

### 3. Testing and GREEN

Testing owns RED and explicitly routed `test_fix` work; Coordinator owns GREEN. Hermes does not perform either role's semantic work. The Executor mechanically validates Testing completion, so Hermes should treat failed acceptance as unresolved specialist ownership rather than commit or reinterpret the result.

Classify test-looking runs by purpose, not command syntax or name. Ordinary deterministic targeted/full tests may be bridge mechanics. Ad-hoc live diagnostics may be run when otherwise permitted, but their output is diagnostic evidence only.

### 4. Review before required external verification

When GREEN and ordinary deterministic tests are ready, Coordinator sends `review_scope: "full"`. Full Review covers the normal requirement/scope, complete code/test diff, regressions, tests, and PR description. Required external-verification evidence is not required for this Review and is not part of its certification.

If Full Review finds a blocking defect, route the correction through the normal Testing/GREEN path and run Full Review again on the changed HEAD. A clean Full Review certification stays attached to that HEAD and reviewed PR-description identity.

### 5. Required external verification

Once a live/external run is required for acceptance or merge readiness, Coordinator may return `VERIFY_EXTERNAL` only after the current HEAD has clean Full Review. Direct ordinary terminal output cannot satisfy that gate. A Testing sandbox limitation on an already-existing suite does not make Testing its mechanical runner when no Testing-owned test/harness work remains.

Required external verification is one non-destructive command/suite against the committed, fully reviewed candidate HEAD. Resolve the pending Executor action either by running it on the Hermes host or, if Hermes cannot safely/correctly do so, by reporting unavailability through the Executor. Agent sandbox limitations alone do not justify asking the user.

Non-zero, timeout, and command-execution outcomes are evidence for Coordinator; Executor/orchestration failures remain workflow errors. Unavailability does not satisfy the gate. Externally supplied evidence is not mechanically attested. Do not inline secrets in verification commands; use existing environment/config.

Requesting verification, reporting unavailability, or recording replacement evidence does not erase the Full Review certification while HEAD remains unchanged. Same-HEAD replacement external verification does not require another Full Review. A HEAD change invalidates the earlier Full Review for purposes of another required external run and makes prior external evidence stale.

### 6. Evidence-only Review and merge

After required external evidence is recorded for the unchanged fully reviewed HEAD, Coordinator sends `review_scope: "external_evidence"`. This Review certifies only the exact external-verification evidence: correct HEAD, command/boundary, provenance, execution/result, and whether the evidence proves the required verification passed. It must not re-review code, tests, coverage, implementation, the full diff, or PR-description content.

The Full Review certification remains the code/test certification. Evidence-only `REVIEW_CLEAN` adds certification of the exact current external evidence; it does not replace the Full Review certification. If evidence is inconclusive or insufficient, retry same-HEAD verification as appropriate. If the run proves a current-change defect and HEAD changes, return to Full Review before rerunning external verification.

Tasks with no required external verification go directly from clean Full Review to merge readiness and gain no second Review.

After the required Review certification(s) are accepted, finish remaining ordinary tests/CI and mark the Draft PR ready when appropriate. Coordinator then makes the final merge-readiness judgment. On `AWAIT_USER_MERGE`, the Executor re-checks the current merge gates, including current Task Review/Full Review certification, PR/local HEAD consistency, PR-description identity, exact evidence-only certification when external verification is required, clean worktree, and `draft=false`.

Never merge without explicit user approval. Merge with `reviewed_head` as the expected-HEAD precondition; if the PR HEAD moved, do not merge and return the mismatch evidence to Coordinator.

### 7. User decisions

On `AWAIT_USER_DECISION`, ask the user and pass the exact answer back through the same workflow. This path is available only when no specialist or Executor action remains unresolved. If Coordinator includes structured external-verification metadata because the user/operator must execute the run, return their result through the same workflow so the Executor preserves it as externally supplied evidence.

When the workflow is pending on the User, classify the reply before acting. If an `AWAIT_USER_MERGE` reply explicitly approves merge, use the merge path. If it instead asks for more investigation, testing, or modification of the same task/PR, resume Coordinator with the exact reply; Hermes must not do that semantic work directly. An unrelated request must not consume or replace the pending workflow answer.

### 8. Recover failures

`ERROR` is not an agent result; never reinterpret partial output as accepted work. Do not commit reported `unverified_artifacts`.

A specialist timeout, `BLOCKED`, malformed/invalid output, non-zero exit, or failed mechanical acceptance leaves that specialist handoff unresolved. Restore/discard failed invocation leftovers, then give decisive failure evidence to read-only Coordinator. While specialist ownership is unresolved, recovery Coordinator may only route focused specialist work or return `BLOCKED`; it cannot wait on a user decision.

If a dispatch bridge/trace fails, keep the accepted pending handoff and retry after fixing the bridge; do not reconstruct the handoff. Report unrecoverable Coordinator failure to the user.

## Invocation

Pending agent invocation, where semantic content already comes from `pending.payload`:

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|task_review|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --timeout-seconds 1800
```

For an initial Coordinator task, recovery evidence, or a user answer, add `--task '<fresh semantic input>'`.

Run pending required external verification:

```bash
python3 <skill-dir>/run_codex.py \
  --run-external-verification \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --timeout-seconds 1800
```

If Hermes cannot safely/correctly execute that request:

```bash
python3 <skill-dir>/run_codex.py \
  --external-verification-unavailable '<reason>' \
  --workflow <workflow-id> \
  --repo <target-repo>
```

Run long-lived role invocations as background jobs with completion notification.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

The opt-in long-running smoke harness must be primed with a legal pending Testing handoff; see `tests/smoke_long_running_invocation.py`.
