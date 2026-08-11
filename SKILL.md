---
name: multi-agent-coding
description: Orchestrate a minimal tests-first coding workflow across separate Codex Testing, Coordinator, and Review agents.
version: 0.1.0
metadata:
  hermes:
    tags: [coding, codex, multi-agent, tdd]
    category: development
    requires_toolsets: [terminal]
---

# Multi-Agent Coding

## When to Use

Use this skill when the user asks Hermes to implement a code change using the multi-agent coding workflow.

This MVP intentionally supports one sequential workflow at a time:

`Testing -> Coordinator -> Review -> user merge decision`

Do not add parallel execution, webhooks, a database, generic adapters, or automatic merge behavior.

## Preconditions

1. The target repository is available locally and has a clean worktree.
2. `codex` is installed and authenticated.
3. Create and switch to a dedicated feature branch before any code change.
4. Locate the directory containing this `SKILL.md`; `run_codex.py` and `prompts/` are sibling paths.
5. Choose a stable workflow id, normally `issue-<number>` or `pr-<number>`.

## Procedure

### 1. Testing / RED

Invoke the Testing agent first:

```bash
python3 <skill-dir>/run_codex.py \
  --agent testing \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<concrete testing task and acceptance criteria>'
```

Require `status=RED_COMPLETE`. The Testing agent owns test intent and must not modify production code.

Before continuing, Hermes must verify mechanically:

- the reported RED commit exists and is current;
- changed files are tests/fixtures only for the target repository;
- the reported targeted test command fails for the expected missing behavior.

If any RED verification fails, stop. Do not route to Coordinator.

Push the branch and open a draft PR after a valid RED commit if no PR exists yet. Then publish the verified Testing handoff comment described below before invoking Coordinator.

### 2. Coordinator / GREEN

Invoke Coordinator only after RED is verified:

```bash
python3 <skill-dir>/run_codex.py \
  --agent coordinator \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<RED commit, failing test evidence, acceptance criteria, and implementation task>'
```

Require `status=GREEN_COMPLETE`.

Hermes must then verify mechanically:

- the reported GREEN commit exists and is current;
- the RED test intent was not rewritten to make the test easier to pass;
- the targeted test command passes;
- the repository's full test suite / CI passes when available.

If verification fails, stop and report the failure. After successful GREEN verification, publish the Coordinator handoff comment described below before invoking Review.

### 3. Fresh Review

Invoke Review after GREEN verification:

```bash
python3 <skill-dir>/run_codex.py \
  --agent review \
  --workflow <workflow-id> \
  --repo <target-repo> \
  --task '<base SHA, current HEAD, acceptance criteria, PR description, and review scope>'
```

Review is intentionally a new Codex session every time. It must not modify files.

For this MVP:

- `REVIEW_CLEAN` -> publish the Review handoff comment, then continue to the merge gate.
- `CHANGES_REQUIRED` -> publish the Review handoff comment with the confirmed findings, then stop and show the findings to the user.
- any other status -> stop. Do not yet implement an automatic repair/re-review loop.

### 4. Merge Gate

When and only when RED is verified, GREEN is verified, full tests/CI pass, and Review returns `REVIEW_CLEAN` for the current HEAD:

1. Update the PR description so it reflects the actual implementation and test evidence.
2. Tell the user the PR is ready and identify the reviewed HEAD SHA.
3. Ask whether to merge.
4. Never merge without explicit user approval.

## PR Handoff Comments

Hermes publishes the handoff comment only after verification of the agent result and the relevant repository/test evidence. Agents must not post their own handoff comments. Their `HERMES_RESULT` is a machine handoff to Hermes; the PR comment is Hermes's human-readable, verified audit trail.

Publish one new top-level PR Conversation comment for each completed phase. Do not edit a previous phase comment into the next phase.

### Testing handoff

```text
### Testing handoff
Status: RED verified
Commit: <red-sha>
Verification: <targeted test command> fails for the expected missing behavior; RED diff is test/fixture-only.
Next: Coordinator
```

### Coordinator handoff

```text
### Coordinator handoff
Status: GREEN verified
Commit: <green-sha>
Verification: targeted tests pass; full test suite / CI status: <result>.
Next: Review
```

### Review handoff

For a clean review:

```text
### Review handoff
Status: REVIEW_CLEAN
Reviewed HEAD: <head-sha>
Verification: fresh Review session checked the current HEAD against acceptance criteria, RED tests, PR description, and relevant diff.
Next: User merge decision
```

For confirmed defects, use the same heading and include the findings:

```text
### Review handoff
Status: CHANGES_REQUIRED
Reviewed HEAD: <head-sha>
Verification: fresh Review session checked the current HEAD against acceptance criteria, RED tests, PR description, and relevant diff.
Findings: <confirmed findings>
Next: User decision; automatic repair/re-review is out of scope for this MVP.
```

Do not publish a successful handoff comment when Hermes's mechanical verification fails. Stop instead and report the verification failure.

## Agent Session Policy

- Testing: persistent Codex session per workflow id.
- Coordinator: persistent Codex session per workflow id.
- Review: always fresh; no session id is persisted.
- Agent identity is the role name, not the Codex thread id.

Runtime state is stored by `run_codex.py` under `<skill-dir>/state/<workflow-id>.json` unless `--state-file` is supplied.

## Result Contract

Each agent must finish with exactly one line beginning with:

```text
HERMES_RESULT={...}
```

The JSON object must contain at least `status`.

Expected happy-path statuses:

- Testing: `RED_COMPLETE`
- Coordinator: `GREEN_COMPLETE`
- Review: `REVIEW_CLEAN`

Treat missing, malformed, unexpected, or contradictory results as failures. Do not infer success from prose.

## Pitfalls

- Do not let Testing implement production code.
- Do not let Coordinator author or weaken RED test intent.
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

For an end-to-end smoke test, choose a small real issue and confirm Hermes can complete Testing -> Coordinator -> Review without the user sending `your turn` between phases, while each verified phase leaves its own PR handoff comment.
