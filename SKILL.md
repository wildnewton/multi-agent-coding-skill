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
- **Task Review:** independently validates the task contract before implementation begins.
- **Testing:** owns RED intent and test quality after Task Review is clean.
- **Review:** independently reviews the full PR diff at the latest committed HEAD.
- **Executor (`run_codex.py`):** owns deterministic handoff/state/audit mechanics and mechanically enforces the role/gate contracts implemented in code.
- **Hermes:** carries user intent/answers to the Executor and owns remaining branch/commit/push/CI/PR/merge mechanics that are not yet inside the Executor.

Coordinator and Testing persist per workflow; every Task Review and Review is fresh. Only Coordinator chooses semantic routing. Task Review, Testing, and Review always return to Coordinator.

The MVP is sequential: one outstanding workflow handoff and at most one role invocation in flight.

## Preconditions

1. Target repository is available locally with a clean worktree.
2. `codex` is installed/authenticated.
3. `gh` is installed/authenticated when GitHub Issue/PR operations are required.
4. `run_codex.py` and `prompts/` are siblings of this file.
5. Use a stable workflow id, normally `issue-<number>` or `pr-<number>`. Use `issue-<number>` when Task Review traces must bind to a canonical Issue.

## Core invariants

- Agents never mutate git or GitHub state. Task Review and Review are repository-read-only. Before Task Review is clean, Coordinator is repository-read-only.
- New semantic code-change tasks must pass fresh Task Review before Testing, GREEN implementation, or Code Review.
- Coordinator is the only semantic routing hub. Specialists never choose `next_agent`.
- The Executor stores one outstanding handoff:

```text
pending = {
  from,
  to,
  payload
}
```

`pending` means only that this exact handoff has not yet been consumed by its receiver.

- Coordinator specialist handoffs become `Coordinator -> Specialist`; a mechanically accepted specialist completion becomes `Specialist -> Coordinator` with the exact accepted structured result. Hermes does not reconstruct specialist tasks/results and there is no `--completed-agent` lifecycle.
- Specialists are invoked from the exact current pending payload. Task Review certification is derived from the canonical task in that payload, not from a second Hermes-supplied copy.
- Testing `RED_COMPLETE` is accepted only after the Executor re-runs the reported `test_command` and confirms it is still non-zero.
- Specialist timeout, `BLOCKED`, non-zero invocation exit, or other failed completion does not consume the original specialist handoff. Recovery Coordinator remains read-only while that specialist ownership is unresolved.
- Task Review clean certification persists after its result handoff is consumed. A new Task Review handoff invalidates prior Task Review and Review certification.
- `REVIEW_CLEAN` certifies the actual reviewed HEAD and current PR-description identity. New Review invalidates prior Review certification. Stale HEAD or PR description blocks merge readiness.
- Every formal agent handoff is durably traced from the exact accepted payload plus Executor-observed mechanical facts. Task Review traces go to the canonical Issue; Testing/Review traces use the Issue before a PR exists and the PR afterward. Earlier traces are not backfilled. User semantic answers are not automatically published.
- Audit publication is fail-closed but not exactly-once. Rare duplicate comments do not justify handoff IDs, transition history, or a deduplication subsystem.
- Never merge without explicit user approval.

`run_codex.py` and its runtime tests are authoritative for detailed transition/state validation; do not duplicate that logic here.

## Invocation

Invoke roles through the same runner:

```bash
python3 <skill-dir>/run_codex.py \
  --agent <coordinator|task_review|testing|review> \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<initial user task, recovery evidence, or user answer>' \
  --timeout-seconds 1800
```

Hermes may still specify the role for each small step, but the Executor rejects any role that is not the current legal receiver. Specialist role prompts come from `pending.payload`, so Hermes does not resupply or rewrite the specialist task.

For Coordinator, `--task` carries the initial user task, recovery evidence, or a user answer when the workflow is waiting on the user. When Coordinator is consuming a completed specialist handoff, the Executor supplies the exact specialist result automatically.

## Procedure

### 1. Coordinator -> Task Review -> Coordinator

Start Coordinator with the user request and decisive repository evidence. Before Task Review is clean, Coordinator may inspect/diagnose, refine the canonical task, ask a real user decision, block, or hand off to Task Review, but may not leave production/test edits.

A Task Review handoff requires `next_agent=task_review`, non-empty `task`, and non-empty `reason`. The task must contain the complete canonical requirement/AC/scope and decisive evidence.

Task Review is fresh and read-only. It returns `TASK_REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`. Completed results carry:

- `evidence_and_root_cause`
- `clearer_requirement`
- `acceptance_criteria`
- `simplest_approach`

On `CHANGES_REQUIRED`, Coordinator revises the canonical task and routes the revision to a fresh Task Review. Repeat until clean.

### 2. Coordinator -> Testing -> Coordinator

Testing is closed until Task Review is clean. A Testing handoff requires `next_agent=testing`, non-empty `task`, and non-empty `reason`.

Testing owns test/test-fixture/test-helper changes and reports `RED_COMPLETE` with the complete current `test_command`. The Executor mechanically reruns that command; only a still-failing command completes the Testing handoff. Timeout/`BLOCKED`/failed acceptance leaves the original Testing handoff unresolved.

After accepted RED, Hermes may commit/push the test-only changes and open/update the draft PR as needed. Coordinator receives the exact Testing result through the pending handoff and decides whether more Testing is required or GREEN can proceed.

### 3. GREEN -> Review -> Coordinator

After clean Task Review and sufficient executable coverage, Coordinator implements the smallest GREEN without weakening test intent.

A Review handoff requires non-empty `task`/`reason` plus exactly one of `full_test_command` or `full_test_unavailable_reason`. Before invoking Review, complete the applicable targeted/full-suite/CI checks and ensure the PR description accurately reflects the current change.

Review is fresh and read-only. `REVIEW_CLEAN` or `CHANGES_REQUIRED` returns directly to Coordinator through the pending handoff. `REVIEW_CLEAN` records the actual reviewed HEAD and PR-description identity; it does not by itself authorize merge.

On `CHANGES_REQUIRED`, Coordinator chooses the smallest justified correction: direct GREEN fix when behavior is already pinned, Testing for missing/incorrect executable intent, Task Review if the canonical task changed materially, or a user decision for genuine product/domain choices.

### 4. User decision

`AWAIT_USER_DECISION` must include a specific non-empty `question`. Hermes asks the user and passes the exact answer back on the next Coordinator step. The Executor persists that answer as `User -> Coordinator` before invoking Coordinator, so a retry reuses the exact accepted answer. Ordinary user answers are not automatically written to GitHub.

### 5. Merge gate

Only Coordinator may return `AWAIT_USER_MERGE`, with `reviewed_head` and `draft=false`.

Merge readiness requires at minimum:

- clean Task Review certification;
- no unresolved specialist ownership;
- current HEAD matches clean Review certification;
- current PR description matches the Review-certified description;
- actual GitHub PR HEAD matches `reviewed_head` before the merge-approval handoff is created;
- required tests/CI/external gates are satisfied;
- explicit user approval before merge.

Final capability isolation—making Hermes technically unable to bypass raw git/GitHub/merge privileges—is outside this MVP and should be handled separately.

## Result contract

Every agent ends with exactly one `HERMES_RESULT={...}` line. Do not infer success/routing from prose.

- **Coordinator:** `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED`.
  - `HANDOFF`: `next_agent` (`task_review|testing|review`), non-empty `task`, non-empty `reason`; Review additionally requires exactly one full-suite field.
  - `AWAIT_USER_DECISION`: non-empty `question`.
  - `AWAIT_USER_MERGE`: non-empty `reviewed_head`, `draft=false`.
- **Task Review:** `TASK_REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`; completed statuses carry the four Task Review fields above.
- **Testing:** `RED_COMPLETE` with non-empty `test_command`, or `BLOCKED`.
- **Review:** `REVIEW_CLEAN`, `CHANGES_REQUIRED`, or `BLOCKED`.
- No agent may include `commit`. Task Review/Testing/Review must not include `next_agent`.

Malformed, contradictory, or role-incompatible results fail closed. If `run_codex.py` reports `unverified_artifacts`, do not commit or reinterpret them as agent output.

## Verification

For this skill repository:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

The opt-in long-running smoke harness must be primed with a legal pending Testing handoff; see `tests/smoke_long_running_invocation.py`.
