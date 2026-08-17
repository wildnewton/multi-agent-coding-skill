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
- New semantic code-change work must pass fresh Task Review before Testing, GREEN, or Review.
- Workflow transport is one durable outstanding handoff: `pending = { from, to, payload }`.
- Specialists are invoked from the exact pending payload. Accepted specialist results return to Coordinator as the exact reverse handoff; Hermes never reconstructs specialist tasks/results and there is no `--completed-agent` lifecycle.
- Testing `RED_COMPLETE` is mechanically accepted only while its reported `test_command` still fails.
- Specialist timeout, `BLOCKED`, malformed/invalid output, non-zero exit, or failed mechanical acceptance leaves the original specialist handoff unresolved. Recovery Coordinator remains read-only.
- Task Review clean certification persists as the accepted task checkpoint. Review clean certification persists as reviewed HEAD + PR-description identity. Stale certification blocks merge readiness.
- Formal agent handoffs and workflow-relevant specialist failures are traced by the Executor to the canonical Issue/PR. Ordinary user answers are not automatically published.
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

## Workflow

1. **Coordinator -> Task Review -> Coordinator**  
   Start from the user request and decisive repository evidence. Coordinator sends the complete canonical task to fresh Task Review and revises/repeats on `CHANGES_REQUIRED` until clean. No implementation/test edits are allowed before clean Task Review.

2. **Coordinator -> Testing -> Coordinator**  
   For executable behavior needing new/corrected test intent, Coordinator hands off to Testing. Testing owns test/fixture/helper edits; the Executor mechanically verifies the reported RED command before accepting the exact result. Hermes may then commit/push RED changes and create/update the Draft PR.

3. **GREEN -> Review -> Coordinator**  
   Coordinator implements the smallest GREEN. Hermes performs applicable commit/push/test/CI/PR-description mechanics, then Coordinator hands the current change to a fresh Review. `REVIEW_CLEAN` certifies the reviewed HEAD and PR description; it does not authorize merge. Fixes require fresh Review again.

4. **User decision**  
   On `AWAIT_USER_DECISION`, Hermes asks the user and passes the exact answer back. The Executor persists `User -> Coordinator` before resuming Coordinator so retry reuses the accepted answer.

5. **Merge approval**  
   `AWAIT_USER_MERGE` is valid only after semantic readiness. The Executor mechanically requires valid Task Review/Review certifications, no unresolved specialist ownership, current local/PR HEAD consistency, and unchanged reviewed PR description. Hermes confirms remaining tests/CI/external gates, then asks the user. Merge only after explicit approval.

Capability-isolating raw git/GitHub privileges behind the Executor is outside this MVP.

## Failure handling

- `ERROR` is not an agent result; never reinterpret partial output as accepted work.
- Do not commit reported `unverified_artifacts`.
- Specialist failure leaves its pending ownership unresolved; give decisive recovery evidence to Coordinator rather than doing specialist work in Hermes.
- Report unrecoverable Coordinator failure to the user.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

The opt-in long-running smoke harness must be primed with a legal pending Testing handoff; see `tests/smoke_long_running_invocation.py`.
