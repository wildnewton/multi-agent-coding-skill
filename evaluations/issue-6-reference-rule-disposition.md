# Issue #6 reference-prompt rule disposition

This records where each meaningful rule from the reference Coordinator and Review prompts belongs after refinement. The goal is to preserve useful engineering judgment without copying Git/GitHub or orchestration mechanics back into semantic-agent prompts.

Disposition labels:
- **Coordinator / Review** — keep as semantic-agent judgment.
- **Hermes / SKILL** — move or keep as orchestration/mechanical responsibility.
- **Shared** — global invariant mechanically enforced by the skill, with role awareness retained in the semantic prompt.
- **Change** — preserve the intent but narrow or correct the rule.
- **Remove** — obsolete or harmful under the current architecture.

## Reference Coordinator prompt

| Reference rule | Disposition | Result |
|---|---|---|
| Senior engineer drives the change end to end | **Change — Coordinator** | Keep semantic ownership of requirement, implementation, triage, and readiness judgment; remove branch/GitHub mechanics from the role. |
| Own branch setup | **Hermes / SKILL** | Hermes creates/switches branch and verifies clean repository state. |
| Own implementation | **Coordinator** | Keep: Coordinator writes the smallest production GREEN after behavior is pinned. |
| Own merge coordination | **Shared** | Coordinator decides when all semantic/external gates are satisfied and asks for merge; Hermes enforces the mechanical gate and performs user-approved merge. |
| Delegate test authorship to Testing | **Shared** | Coordinator decides what must be proven; skill preserves Testing ownership and routes through Hermes. |
| Delegate fresh-eyes review | **Shared** | Coordinator requests Review; skill creates a fresh Review session and prevents self-certification. |
| Step 1: create branch/open draft PR | **Hermes / SKILL** | Pure repository/PR mechanics. |
| Step 2: hand requirement/branch to Testing and wait for RED commit | **Change / Shared** | Coordinator supplies the concrete RED task and reason; Hermes dispatches, verifies RED, creates the RED commit, and resumes Coordinator. |
| Step 3: implement without changing test intent; bad/missing test goes back to Testing | **Shared** | Keep implementation judgment in Coordinator and Testing-ownership awareness in prompt; skill enforces role boundary. |
| Step 4: commit GREEN | **Hermes / SKILL** | Coordinator leaves production edits unstaged; Hermes verifies and creates the commit. |
| Step 5: run targeted/full tests or CI | **Change / Hermes** | Coordinator identifies the relevant full-suite command or absence; Hermes runs/verifies latest targeted + full suite and CI once. Avoid duplicate execution. |
| Step 6: update PR description and hand off issue/AC/diff/description to Review | **Shared** | Coordinator chooses concise semantic review scope/evidence; Hermes updates PR metadata, supplies current HEAD/diff/context, and dispatches Review. |
| Step 7: wait for findings | **Hermes / SKILL** | Transport/control flow; Review result is returned to persistent Coordinator. |
| Step 8: triage implementation/test/risk/question/optional findings | **Change — Coordinator** | Keep and strengthen: classify evidence class separately from severity; only justified items become mandatory work. New behavioral findings go to Testing when focused RED is needed. |
| Step 9: after fix rerun tests and re-review | **Shared** | Coordinator decides another Review is warranted; Hermes performs deterministic verification and dispatches a fresh Review. |
| Step 10: no confirmed defects → ask merge | **Change — Coordinator** | Require clean Review **and** satisfied required external/manual gates before `AWAIT_USER_MERGE`. |
| Step 11: squash merge and close issues | **Hermes / SKILL** | Mechanical action only after explicit user approval and satisfied gates. |
| Never author/edit RED test logic | **Shared** | Skill enforces Testing ownership; Coordinator retains awareness and routes test corrections back. |
| Never self-certify review | **Shared** | Skill creates fresh Review; Coordinator prompt explicitly forbids self-review. |
| PR work-summary comment + checkpoint/title format | **Hermes / SKILL** | Coordinator supplies semantic reason/task/evidence; Hermes formats and publishes the verified audit comment. |
| Hand off to Hermes only when connector blocks GREEN | **Remove** | Obsolete. Hermes is always the normal dispatcher/verifier and git/GitHub owner, not a fallback. |

## Reference Review prompt

| Reference rule | Disposition | Result |
|---|---|---|
| Interact only with implementation agent; no Testing contact | **Change / Shared** | Current semantic destination is Coordinator; topology is enforced by SKILL/Hermes. Review only returns structured findings. |
| Independent senior fresh-eyes reviewer; do not implement | **Shared** | Review prompt keeps independent reasoning; skill guarantees fresh session/read-only execution. |
| Review production code and tests | **Review** | Keep. |
| Do not modify code/tests, merge, close issues, or write tests | **Shared** | Review prompt states read-only role; skill/runtime enforces no file/git mutation and routes findings through Coordinator. |
| Expect issue/AC, diff, surrounding code, PR description | **Shared** | Hermes supplies current verified inputs; Review consumes them. |
| Re-read requirement/AC and verify minimal viable change/no unrelated behavior | **Review** | Keep and strengthen with pinned scope and latest HEAD. |
| Review diff/surrounding code for logic, edge cases, regressions, assumptions, validation/failure handling, complexity | **Review** | Keep as core review method. |
| Find omissions elsewhere where same/similar fix may be required | **Change — Review** | Narrow to the same requirement/invariant/root cause that would leave the current change incomplete. Similar unrelated debt stays non-blocking/out of scope. |
| Search rest of codebase for same/similar bug | **Change — Review** | Keep as bounded investigation heuristic, not automatic scope expansion. |
| Review tests critically, not line coverage | **Review** | Keep; distinguish a test gap from a production defect. |
| Confirm smallest reasonable change | **Review** | Keep; judge against pinned requirement rather than personal architecture preference. |
| Check PR description accuracy | **Shared** | Review judges semantic accuracy; Hermes/Coordinator performs metadata mutation. |
| Post review as PR comments | **Hermes / SKILL** | Review returns machine result; Hermes publishes verified human-readable handoff. |
| Comment title/checkpoint/HEAD formatting | **Hermes / SKILL** | Mechanical audit-trail concern. |
| Findings grouped as Confirmed Defect / Risk / Question / Optional Improvement | **Change — Review** | Expand evidence taxonomy to production defect, test gap, validation gap, suspicion, risk, question, optional improvement, external gate, PR-description defect; severity and confidence remain separate. |
| For each finding give problem, why it matters, smallest fix | **Change — Review** | Keep problem/evidence/impact; replace implementation prescription with smallest required behavior/remediation boundary unless implementation detail is necessary for safety/clarity. |
| Line-change summary split production/tests | **Hermes / SKILL** | Mechanical diff statistics should come from the source of truth when needed, not consume Review judgment. |
| Top-line approve / approve-with-minor-notes / changes-required verdict | **Review** | Keep and tighten: `CHANGES_REQUIRED` only for confirmed blockers or unvalidated required code-review AC; code approval can coexist with external/manual gates. |
| Do not modify code/tests | **Shared** | Retain role awareness and mechanical read-only enforcement. |

## Post-refinement static scenario replay

The refined prompts/SKILL were checked against the curated scenarios without adding a benchmark framework or new runtime status:

| Scenario | Expected result after refinement |
|---|---|
| S1 — pending freshness defect + parser risk | Review: blocking `production_defect` + `test_gap`, parser remains `risk`; Coordinator routes focused behavior to Testing, not parser work. |
| S2 — KY/foreign interpretation later withdrawn | The initial nationality interpretation is disproven by the corrected listing-market requirement and deterministic OTC-listed foreign issuer `6741` evidence, so Review must withdraw it rather than preserve a blocker. Only the actual listing-status defect proceeds to Testing. |
| S3 — reconnect/protocol/quota defects plus ACK risk/Abort question | Review: three blockers, plus non-blocking `risk`/`question`; Coordinator sends only executable blockers to Testing unless requirement evidence promotes the notes. |
| S4 — terminal-event race / evidence classifier | Review: concrete violated behavior with `production_defect`; Coordinator requests focused RED and then smallest GREEN. |
| S5 — code clean but Nova live probe outstanding | Review: `REVIEW_CLEAN` + `APPROVE_WITH_MINOR_NOTES` and `external_gate`; Coordinator does not return merge-ready until the required live evidence is supplied. PR may remain Draft while code review is clean. |

No scenario requires a new routing status. Existing `REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED`, `AWAIT_USER_DECISION`, and `AWAIT_USER_MERGE` are sufficient when judgment and gate semantics are explicit.

**Remaining evaluation gap:** S2 tests false certainty through a disproven requirement interpretation; it is not a true attempted reproduction that fails to reproduce. Issue #6's explicit non-reproduction scenario acceptance criterion remains open until a real evidence-backed case is added.
