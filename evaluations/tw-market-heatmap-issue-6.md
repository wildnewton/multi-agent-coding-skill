# Issue #6 evaluation corpus: Coordinator and Review judgment

This document is the evidence baseline for refining `prompts/coordinator.md`, `prompts/review.md`, and only the SKILL-level rules that are genuinely orchestration concerns.

The goal is not to create a benchmark framework. These scenarios are small, concrete decision fixtures from `wildnewton/tw-market-heatmap` that expose judgment errors we want the prompts to avoid.

## Confirmed current gap

The current Coordinator prompt already owns semantic routing and minimal implementation, but it does not explicitly pin requirement/scope/external gates before acting or classify Review findings by evidence class.

The current Review prompt asks for confirmed issues but its result contract is effectively binary (`REVIEW_CLEAN` vs `CHANGES_REQUIRED`) and does not represent test gaps, unconfirmed suspicions, non-blocking risks, optional improvements, or external/manual gates separately.

## Scenarios

### S1 — PR #34: pending quote poll freezes freshness

**Evidence**

Fresh-eye Review found that after one valid snapshot, a later request that remains pending can keep `quotePollInFlight = true`; the cadence returns before advancing `nowMs`, so stale-state UI can remain visually fresh forever. Review also noted a separate parser invariant as a risk, not a confirmed blocker.

The next handoff requested one focused Testing regression: accept a valid snapshot, leave the next request pending, advance fake time beyond 15 seconds, preserve the heatmap, and require the delayed warning. Testing reproduced the defect with one targeted RED.

References:
- `tw-market-heatmap` PR #34 review around `discussion_r3724703236` / review `4869547853`
- Testing follow-up comment `5199037488`
- RED evidence comment `5199102479`

**Expected Review judgment**

- Confirmed production defect: pending request can freeze freshness.
- Confirmed test gap: no regression covers the pending/hung request path.
- Risk: parser semantic invariant remains non-blocking unless separately shown to violate the pinned requirement.
- Verdict: `CHANGES_REQUIRED` because a confirmed blocking defect exists.

**Expected Coordinator decision**

Route the behavioral finding to Testing for one focused regression RED before implementation. Do not convert the separate risk into mandatory scope.

**Failure modes exposed**

- Fixing a newly discovered behavioral defect directly without pinning it in Testing-owned RED intent.
- Turning every Review note into blocking work.
- Broadening a single freshness bug into unrelated parser work.

### S2 — PR #48: listing eligibility and false certainty

**Evidence**

Review initially claimed that foreign issuers/KY shares should be excluded from canonical membership. A later correction explicitly withdrew that part: the actual rule is listing market/status — keep TWSE/TPEx-listed securities including foreign issuers/KY; exclude emerging-board securities. The surviving confirmed defect was that `is_foreign` is not a valid listing-status predicate, so an emerging-board security such as 7781 could enter the canonical universe.

Coordinator then routed the clarified market-status defect to Testing, which produced deterministic RED cases showing an emerging security must be excluded while an OTC-listed foreign issuer must remain included.

References:
- `tw-market-heatmap` PR #48 corrected review `4890506910`
- Coordinator → Testing comment `5229775327`
- Testing RED comment `5229821524`

**Expected Review judgment**

Before the market-status rule is pinned, do not present issuer nationality as a confirmed defect. Classify the ambiguity as a question/unconfirmed interpretation. Once the requirement is clarified, confirm only the listing-status defect supported by evidence.

**Expected Coordinator decision**

Do not implement the reviewer's initial nationality interpretation. Resolve the requirement first; then route the clarified executable defect to Testing.

**Failure modes exposed**

- False certainty: treating an interpretation as a confirmed defect.
- Converting reviewer preference/assumption into requirement.
- Failing to narrow or withdraw a finding when later evidence disproves part of it.

### S3 — PR #51: reconnect defects vs non-blocking risk/question

**Evidence**

Fresh-eye Review identified three concrete blocking defects: no automatic reconnect after a ready-stream disconnect, protocol-level `event:"error"` could stall the state machine, and a single connection could be configured above the conservative 300-symbol capacity. The same review separately listed an ACK-timeout/watchdog concern as a **Risk** and AbortSignal semantics as a **Question**.

The Testing follow-up explicitly requested RED only for the three confirmed defects and said not to add the ACK-timeout risk or AbortSignal question as mandatory RED unless the pinned spec already required them.

References:
- `tw-market-heatmap` PR #51 fresh-eye review `4880012130`
- Testing follow-up in the same review cycle

**Expected Review judgment**

- Confirmed production defects: reconnect, protocol error handling, >300 single-connection capacity.
- Confirmed coverage gaps: tests allow those defects through.
- Non-blocking risk: missing ACK timeout/watchdog.
- Question: AbortSignal semantics.
- Verdict: `CHANGES_REQUIRED` only because of the confirmed defects.

**Expected Coordinator decision**

Route the three executable defects to Testing for focused RED. Do not manufacture mandatory RED for the risk/question unless requirement evidence promotes them.

**Failure modes exposed**

- Risk/question silently becoming requirement.
- Coverage gap being mislabeled as a production bug without the production failure path.
- Over-scoping a focused recovery fix into a generic resilience framework.

### S4 — PR #52: live-probe race and evidence classifier defects

**Evidence**

Review found concrete false-signoff paths, including a terminal error race after unsubscribe ACK and a classifier that could turn unrelated subscription failures into `account-wide-300`. Testing then added focused review-regression tests; the new tests failed for the intended current behavior, including `pass` instead of `fail` for the terminal race and `account-wide-300` instead of `inconclusive` for an unrelated subscription error.

References:
- `tw-market-heatmap` PR #52 race review cycle
- Testing regression RED `409bef25b32334c3eee689760799d8173fb72b7f`

**Expected Review judgment**

These are confirmed behavioral defects because each has a violated acceptance rule and a concrete failure path. Describe the violated behavior and remediation boundary; do not prescribe an unnecessary implementation architecture.

**Expected Coordinator decision**

Route the behavior to Testing for focused regression RED, then implement the smallest GREEN consistent with the pinned evidence.

**Failure modes exposed**

- Calling a plausible race a confirmed defect without a concrete invariant/failure path.
- Prescribing implementation rather than the required behavior boundary.
- Skipping Testing when a new executable behavioral contract is needed.

### S5 — PR #52 final: code clean, merge still blocked by external gate

**Evidence**

At final review HEAD `4901f404...`, fresh-eyes code review found no confirmed code defect and CI was green. The Coordinator checkpoint explicitly stated this was still insufficient for merge: Issue #50 required an opt-in credentialed Nova probe with sanitized evidence for six live observations. The PR remained draft until that manual/external gate was satisfied.

Reference:
- `tw-market-heatmap` PR #52 Coordinator checkpoint comment after final re-review (`merge blocked pending Taishin Nova live smoke`)

**Expected Review judgment**

Approve the reviewed code when there are no confirmed blocking defects. Report the credentialed probe as an external/manual gate, not as a code defect and not as a reason to invent more code work.

**Expected Coordinator decision**

Do not return merge-ready yet. Code GREEN + Review clean + CI green are necessary but not sufficient while a required external/manual acceptance gate remains open.

**Failure modes exposed**

- `REVIEW_CLEAN` being interpreted as merge-ready.
- External validation being mislabeled as code defect.
- Over-review: inventing extra work instead of approving a clean HEAD.

## Failure modes to address before changing prompts

### Coordinator

1. **Requirement drift** — optional idea, risk, or reviewer suggestion becomes a requirement without evidence.
2. **Premature GREEN** — a newly discovered behavioral defect is fixed directly when focused Testing RED is needed to pin the contract.
3. **Role absorption** — Coordinator rewrites Testing-owned RED intent instead of routing the test concern back.
4. **Scope expansion** — one finding becomes a generic refactor/framework or unrelated cleanup.
5. **Weak finding triage** — production defect, test gap, suspicion, risk, preference, and external gate are treated as equivalent.
6. **Premature merge readiness** — code/review/CI green is treated as sufficient despite an unresolved required manual/external gate.
7. **Evidence dumping** — handoff passes history instead of the minimum requirement/finding, evidence, scope boundary, task, and unresolved gate needed by the next role.

### Review

1. **Binary certainty** — every concern is forced into clean vs blocking defect.
2. **False certainty** — requirement interpretation or suspicion is presented as confirmed before evidence supports it.
3. **Test/production conflation** — a missing test alone is called a production bug.
4. **Preference as blocker** — architecture/style preference creates `CHANGES_REQUIRED` without a violated requirement/invariant.
5. **Automatic scope expansion** — similar-looking adjacent debt expands the PR without showing the same root cause leaves the current requirement incomplete.
6. **Over-prescription** — finding dictates production design instead of the violated behavior/invariant and remediation boundary.
7. **Stale re-review** — prior disproven findings are reopened or the verdict is not scoped to latest HEAD.
8. **Approval conflation** — code approval is treated as satisfying user approval or external/manual acceptance gates.

## Responsibility decomposition

### Coordinator semantic judgment

Keep in Coordinator:
- pin requirement, acceptance criteria, confirmed gap, scope boundaries, dependencies/external gates, and missing evidence;
- choose the smallest justified semantic next action;
- decide what Testing needs to prove and preserve Testing-owned RED intent;
- implement the smallest production GREEN once behavior is pinned;
- classify Review findings and decide Testing vs implementation vs no action vs user/domain decision;
- judge whether external/manual gates still block asking for merge;
- compress the semantic handoff to the evidence the next role needs.

### Review semantic judgment

Keep in Review:
- independently judge latest supplied HEAD against pinned requirement/acceptance criteria/scope;
- inspect production code, tests, and relevant surrounding code;
- distinguish confirmed production defects, confirmed test gaps, unconfirmed suspicions, non-blocking risks/questions, optional improvements, and external/manual gates;
- verify prior findings on re-review and avoid reopening disproven findings without new evidence;
- investigate similar code only far enough to determine whether the same root cause/invariant leaves the current requirement incomplete;
- describe violated behavior/invariant and remediation boundary without unnecessary implementation prescription;
- approve when no confirmed blocking defect remains, while reporting non-blocking notes/gates separately.

### Skill / Hermes orchestration

Keep out of semantic prompts where possible:
- branch/PR/commit/push/comment/draft/merge mutations;
- clean-worktree and HEAD/ref verification;
- agent dispatch and result transport;
- persistent Coordinator/Testing sessions and fresh Review sessions;
- latest-HEAD mechanical verification;
- deterministic RED/GREEN/CI/merge-gate execution;
- PR comment/checkpoint formatting and publication;
- final squash merge/issue closure after explicit user approval and satisfied gates.

### Shared invariants, different levels

- Testing owns RED intent: skill enforces it; Coordinator reasons with it.
- Review owns fresh-eyes certification: skill creates independent Review; Review reasons independently.
- Coordinator owns semantic routing: Coordinator chooses; Hermes validates/executes.
- Review findings return through Coordinator: skill enforces topology; Coordinator triages content.
- Merge requires user approval and satisfied gates: Coordinator decides when it is appropriate to ask; Hermes blocks/executes the mechanical merge.

## Prompt-refinement target

A successful refinement should make the expected decisions above more likely without adding a new runtime state machine, generic evaluation platform, or extra agent role. Prompt/SKILL judgment changes should be evaluated against these scenarios directly; executable tests are needed only if runtime behavior changes.
