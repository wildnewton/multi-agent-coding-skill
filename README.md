# Multi-Agent Coding Skill（多智能體編碼技能）

這是一個給 Hermes 使用的多智能體編碼 Skill，用來協調多個 Codex agent 執行 **task-reviewed、tests-first** 的軟體開發流程，同時把語義路由、機械驗證、Git/GitHub 操作與最終合併權限分開。

目前工作流包含四個專門 agent，以及兩層 orchestration：

- **Coordinator（協調者）** — 語義上的路由中心。負責 canonical task、requirements / scope、production implementation / GREEN、finding triage 與 merge-readiness 判斷；只有它可以決定下一個 specialist agent。
- **Task Review（任務審查）** — 在 implementation 前獨立驗證 task / requirement / acceptance criteria；每次都是全新的唯讀審查。
- **Testing（測試）** — 負責 RED test intent、明確授權的 test-only correction 與 test quality。可以修改 tests、fixtures 與 test-only helpers，但不負責 production implementation。
- **Review（審查）** — 對最新 committed HEAD 的完整 PR diff 進行全新的唯讀審查，並回傳 findings 或 clean certification。
- **Executor（`run_codex.py`）** — 負責 deterministic handoff、workflow state、result-contract validation、dispatch audit 與 mechanical gates。
- **Hermes** — 負責 user-facing transport，以及 Executor 之外的 branch / commit / push / test / CI / Draft PR / merge 等 Git/GitHub mechanics。

所有 Codex agent 都不能自行 commit、push、切 branch 或修改 remote GitHub state。Task Review、Review，以及 Task Review clean 之前的 Coordinator 都是唯讀；真正的 Git/GitHub mutation 由 Hermes orchestration 執行。

## 為什麼需要這個專案

多智能體 coding workflow 很容易在「每個 agent 都能自行決定下一步」或「agent 的聲明直接被當成事實」時變得脆弱。這個專案刻意把**語義路由集中在 Coordinator**，把**可重現的 mechanical checks 與 durable handoff state 放進 Executor**，再由 Hermes 執行必要的外部 mechanics。

目標是形成這樣的工作流：

1. 需求由 Coordinator 統一理解與分派；
2. 新的 semantic code-change work 必須先經 Task Review；
3. executable behavior 優先由 Testing 建立或修正 RED intent；已確認的既有 test / fixture / test-helper defect 則可經窄義 test-fix path 回 Testing；
4. 每次 agent-to-agent transition 都先被 Executor 接受並持久化，再進行必要的 Git/GitHub bridge，最後才 dispatch 下一個 agent；
5. specialist failure 不會偷偷丟失 ownership；原本的 pending handoff 會保留；
6. Review clean certification 綁定 reviewed HEAD 與 PR description；
7. 每次正式 handoff 都留下可檢查的 Issue / PR audit trace；
8. merge 永遠由使用者明確批准，而且實際 merge 必須綁定已 review 的 PR HEAD。

## 架構

語義上的 routing authority：

```text
使用者
  |
  v
Coordinator
  | \
  |  +--> Task Review
  |  +--> Testing
  |  +--> Review
  |
  +--> 使用者決策 / merge approval
```

Task Review、Testing 與 Review 不會直接互相路由。它們都把結果交回 Coordinator，再由 Coordinator 決定下一個 semantic step。

每個正式 agent-to-agent transition 都經過同一個 transport cycle：

```text
From Agent
    |
    v
Executor: ACCEPT
    |   驗證 result + mechanical evidence
    |   persist single pending {from, to, payload}
    v
Hermes: BRIDGE
    |   必要時 commit / push / tests / CI / Draft PR / PR metadata
    v
Executor: DISPATCH
    |   驗證 pending receiver + dispatch state
    |   發布 handoff trace
    v
To Agent
```

Workflow 只維護**一個 durable outstanding handoff**：`pending = { from, to, payload }`。不另外維護 second pending、phase flag 或 handoff id。

## 工作流程

目前 MVP 刻意維持 sequential execution。

```text
使用者需求
    |
    v
Coordinator
    |
    | HANDOFF -> Task Review
    v
fresh Task Review
    |
    +--> CHANGES_REQUIRED -> Coordinator
    |                          |
    |                          +--> task 仍需 implementation -> 修訂 task -> fresh Task Review
    |                          |
    |                          +--> evidence 證明無需 implementation -> COMPLETED
    |
    +--> TASK_REVIEW_CLEAN
              |
              v
          Coordinator
              |
              +--> executable behavior -> HANDOFF -> Testing
              |                              |
              |                              v
              |                         Testing 建立 RED
              |                              |
              |                         Executor 驗證仍為 RED
              |                              |
              |                         Hermes commit / push
              |                         並在首個真實 implementation commit 後建立 Draft PR
              |                              |
              +<-----------------------------+
              |
              v
        Coordinator 實作最小 GREEN
              |
              | HANDOFF -> Review
              v
        Executor ACCEPT
              |
        Hermes commit / push / tests / CI / 更新 PR description
              |
        Executor DISPATCH
              |
        fresh Review
              |
              +--> CHANGES_REQUIRED -> Coordinator
              |         |
              |         +--> implementation fix
              |         +--> HANDOFF -> Testing
              |         +--> 必要時 user decision
              |
              +--> REVIEW_CLEAN
                        |
                        v
                final tests / CI / external gates
                Draft PR -> ready
                        |
                        v
                    Coordinator
                        |
                AWAIT_USER_MERGE
                        |
                        v
                  使用者明確批准
                        |
                        v
                Hermes merge with reviewed_head
```

`Review -> CHANGES_REQUIRED` 不代表一定回 Testing。Coordinator 會依 finding 的來源決定：已被既有 test intent pin 住的 implementation defect 可以直接修；已確認且正確修復後應保持 GREEN 的既有 test / fixture / test-helper defect，使用 `testing_intent: "test_fix"` + exact `allowed_paths` 回 Testing；test / coverage gap 或需要 executable reproduction 的 regression 則走普通 RED；真正的產品／domain 決策才詢問使用者。

如果 decisive evidence 證明 task 已經不需要 implementation，Coordinator 可以回傳 `COMPLETED`。如果 task 先前已取得 clean Task Review、之後才**實質改變**為「不需要 implementation」，必須先把修訂後的 task 再送一次 fresh Task Review。只要 current `TASK_REVIEW_CLEAN` checkpoint 仍存在，`COMPLETED` 就會被拒絕；fresh Task Review 若確認原 task 已不能照原樣進入 implementation，會以 `CHANGES_REQUIRED` 把 evidence 交回 Coordinator，再由 Coordinator completion。

## Mechanical gates

Executor 不會因為 agent 聲稱「完成」就直接接受 transition。

### Task Review gate

- 新的 semantic code-change work 在 Testing、production implementation 或 Review 前，都必須取得 fresh `TASK_REVIEW_CLEAN`。
- `CHANGES_REQUIRED` 會回到 Coordinator；如果仍需 implementation，Coordinator 修正 canonical task 後再次送 fresh Task Review。
- Task Review certification 以 task checkpoint 持久化；material task change 會使舊 certification 失效。
- Task Review 與 Task Review clean 前的 Coordinator invocation 必須保持 worktree 唯讀。

### RED gate

普通 Testing handoff 回傳 `RED_COMPLETE` 時：

- 必須提供非空的 `test_command`；
- Executor 會實際執行該 command；
- command 必須**仍然失敗**，否則不接受 RED；
- RED verification 有 timeout，且執行過程不得修改 worktree 或 Git state；
- Testing 自己也不能 commit、push 或修改 branch / remote state。

只有 RED 被 mechanically accepted 後，才會建立 `Testing -> Coordinator` pending handoff。

### Test-fix gate

只有 Coordinator 已確認既有 test / fixture / test-helper defect，而且正確修復後 verification 應該通過時，才使用特殊 Testing handoff：

- handoff 必須明確包含 `testing_intent: "test_fix"`；
- 必須提供非空、normalized、repository-relative 的 exact `allowed_paths`；
- Testing 只能以 `TEST_FIX_COMPLETE` 完成，不能假裝成普通 RED；
- Executor mechanically 確認 Testing invocation 的所有 changed paths 都在 `allowed_paths`；
- `test_command` 必須**成功**；
- command verification 同樣有 timeout 與 repository-mutation guard；
- 普通 RED handoff 不接受 `TEST_FIX_COMPLETE`。

這條 path 不新增 GREEN phase/checkpoint，也不靠 path naming heuristic 猜哪些檔案是 production code。有效完成後仍使用既有 `Testing -> Coordinator` pending lifecycle；之後若 HEAD 改變，仍必須 fresh Review。

### Implementation / Review dispatch gate

Coordinator 完成 GREEN 後只能先建立 `Coordinator -> Review` pending handoff；接受 handoff 不等於 Review 已被 dispatch。

在真正 dispatch Review 前，Hermes 先完成需要的 bridge mechanics，例如：

- commit / push current GREEN；
- targeted / full tests；
- CI；
- 建立或更新 Draft PR；
- 更新 PR description，使其與實際 implementation / evidence 一致。

Implementation-stage dispatch 必須在需要 PR 的階段看到實際 PR，而且 GitHub PR HEAD 必須等於 local HEAD。Review clean certification 綁定：

- reviewed HEAD；
- Review dispatch 時的 PR description identity。

Review 過程中若 PR description 改變，clean result 會被拒絕，必須 fresh Review。

### Merge gate

在 `AWAIT_USER_MERGE` 被接受前，Executor 會確認：

- 仍有有效的 Task Review clean checkpoint；
- current HEAD 具有 `REVIEW_CLEAN` certification；
- `reviewed_head` 同時等於 certified HEAD、local HEAD 與實際 GitHub PR HEAD；
- worktree 乾淨；
- PR description 仍與 Review certification 相同；
- 沒有 unresolved specialist handoff；
- 實際 GitHub PR 已經不是 Draft；
- Coordinator result 明確包含 `draft=false`。

使用者批准後，Hermes merge 時仍以 `reviewed_head` 作為 expected-HEAD precondition。若 PR HEAD 已移動，不 merge，而是把 mismatch evidence 交回 Coordinator。

這個 workflow **永遠不會自動 merge**。

## Specialist failure 與 recovery

specialist invocation 若發生以下情況：

- timeout；
- non-zero exit；
- malformed / role-invalid result；
- `BLOCKED`；
- mechanical acceptance failure，例如 RED command 已經變 GREEN、test-fix command 失敗或 test-fix 改到 `allowed_paths` 之外；

原本的 specialist `pending` 會保持 unresolved，而不是被當成完成。Executor 會盡可能留下 specialist failure trace。

Hermes 先清理 failed invocation 留下的未驗證 artifacts，再把 decisive failure evidence 交給 Coordinator。Recovery Coordinator 必須唯讀；在目前 MVP 中只能：

- 重新 HANDOFF 給適當 specialist，以保留或轉移 ownership；或
- 回傳 `BLOCKED`。

只要 specialist ownership 尚未 resolve，就不能 `AWAIT_USER_DECISION`、`COMPLETED` 或 `AWAIT_USER_MERGE`。

## Agent session policy

- **Coordinator** — 每個 workflow id 保留一個 persistent Codex session。
- **Task Review** — 每次都建立全新的 Codex session。
- **Testing** — 每個 workflow id 保留一個 persistent Codex session。
- **Review** — 每次都建立全新的 Codex session。

預設 workflow state 儲存在 `state/<workflow-id>.json`。目前 state 的核心是：

```text
sessions
pending
task_review_clean_checkpoint
review_certification
```

舊的 legacy pending-state format 不再支援；若 state file 仍含舊欄位，應開始新的 workflow，而不是嘗試自動遷移。

## Result contract

每次 Codex agent invocation 結束時，agent final response 必須包含一行 machine-readable result：

```text
HERMES_RESULT={...}
```

每個角色允許的 status：

| Agent | 可接受 status |
| --- | --- |
| Coordinator | `HANDOFF`, `COMPLETED`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED` |
| Task Review | `TASK_REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED` |
| Testing | `RED_COMPLETE`, `TEST_FIX_COMPLETE`, `BLOCKED` |
| Review | `REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED` |

重要限制：

- 只有 Coordinator 可以回傳 `next_agent`。
- Coordinator `HANDOFF` 只能指向 `task_review`、`testing` 或 `review`，並必須包含非空 `task` 與 `reason`。
- 普通 `HANDOFF -> testing` 不包含 `testing_intent` / `allowed_paths`，維持既有 RED semantics。
- 特殊 `HANDOFF -> testing` 只有 `testing_intent: "test_fix"` 合法，並必須包含非空 exact `allowed_paths`；只有此 handoff 可接受 `TEST_FIX_COMPLETE`。
- `HANDOFF -> review` 必須包含且只能包含 `full_test_command` / `full_test_unavailable_reason` 其中之一。
- `COMPLETED` 必須包含非空 `report`，而且只能在沒有 unresolved specialist ownership、沒有 current `TASK_REVIEW_CLEAN` checkpoint、沒有 current implementation-stage PR 的 no-change completion path 使用。
- `AWAIT_USER_DECISION` 必須包含非空 `question`。
- `AWAIT_USER_MERGE` 必須包含 `reviewed_head` 並明確 `draft=false`。
- Task Review 的 clean / changes-required result 必須提供 evidence/root cause、clearer requirement、acceptance criteria 與 simplest approach。
- Review `CHANGES_REQUIRED` 必須包含至少一個 actionable finding。
- 所有 agent 都不得回傳 `commit`；commit 是 orchestration layer 的責任。
- malformed、互相矛盾或與角色不相容的 result 會被拒絕，不會根據 prose 猜測其含義。

## 使用前條件

目標 coding repository 應存在於本機，而且每次 agent invocation 開始時 worktree 必須乾淨。工作流另外需要：

- Python 3；
- 已安裝並完成認證的 `codex` CLI；
- 已安裝並完成認證的 `gh` CLI，而且對目標 repository 有建立／更新 PR、留言與 merge 所需的權限；
- 在第一個**可能修改 repository files** 的 agent invocation 之前，由 Hermes 建立 dedicated feature branch。

Skill repository 本身不需要就是被修改的 repository。`run_codex.py --repo` 會把每個 agent 指向真正的 target coding repository。

Task Review clean 之後不需要製造 empty commit 來開 PR。等第一個真實 implementation-stage commit 出現後——通常是 RED，沒有 RED 時則可能是 GREEN——Hermes push 該 commit 並建立 Draft PR。

## 執行 Agent

`run_codex.py` 是 workflow 的 deterministic Executor，而不只是 session wrapper。

初始 Coordinator：

```bash
python3 run_codex.py \
  --agent coordinator \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Implement issue #123. Acceptance criteria: ...' \
  --timeout-seconds 1800
```

之後 Hermes 依 current `pending` 呼叫合法 receiver，例如：

```bash
python3 run_codex.py \
  --agent task_review \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'dispatch pending handoff'
```

```bash
python3 run_codex.py \
  --agent testing \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'dispatch pending handoff'
```

```bash
python3 run_codex.py \
  --agent review \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'dispatch pending handoff'
```

specialist 的實際 task 來自持久化的 `pending.payload`；`--task` 對 specialist 只是 CLI 必填參數，不是新的 semantic task。`--task` 真正承載內容的情況主要是：

- 初始 Coordinator user task；
- recovery evidence；
- `AWAIT_USER_DECISION` 後的 user answer。

可選參數：

- `--state-file <path>` — 覆寫預設 workflow state file。
- `--prompt-dir <path>` — 覆寫內建 `prompts/`。
- `--timeout-seconds <seconds>` — 覆寫 Codex subprocess timeout；預設為 1800 秒。

長時間 invocation 應由 Hermes 以可追蹤的 background job 執行；process spawn 只代表 dispatch 已開始，不代表 agent 已完成。必須等 `run_codex.py` 真正結束並取得 result 後，才能進行下一次 routing。

## Issue / PR audit trail

Formal handoff trace 在 **DISPATCH 時** 發布，因為只有此時才能確定實際 dispatch HEAD 與 audit destination。

目前位置規則：

- 任何涉及 **Task Review** 的 handoff 都留在 canonical Issue；因此 Task Review workflow 應使用 `issue-<number>` workflow id。
- 其他 formal handoff：PR 尚未存在時留在 Issue；PR 存在後留在 PR。
- 不回填舊 trace。也就是說，最早的 `Coordinator -> Testing` 可能仍在 Issue，而 RED commit 建立 Draft PR 後的 `Testing -> Coordinator` 與後續 implementation-stage trace 會在 PR。
- specialist invocation 被拒絕或失敗時，Executor 會盡可能發布 failure trace，並明確指出 pending handoff 仍 unresolved。

Audit publishing 採 fail-closed：正式 dispatch trace 發布失敗時，不會繼續執行 receiver。MVP 不追求 exactly-once，也不為極少見的重複 comment 增加 handoff id / dedup infrastructure。

## Repository 結構

```text
.
├── SKILL.md                 # Hermes operator contract / workflow specification
├── run_codex.py             # deterministic handoff executor / state / gates
├── prompts/
│   ├── coordinator.md       # Coordinator 角色與 result contract
│   ├── task_review.md       # Task Review 角色與 review contract
│   ├── testing.md           # Testing / RED + test-fix 角色規格
│   └── review.md            # Review 角色與 finding contract
├── state/                   # workflow state（runtime 產生）
├── tests/                   # Executor / workflow regression tests
├── .github/workflows/       # 這個 Skill repository 的 CI
└── README.md
```

`run_codex.py` 與 runtime tests 是 detailed transition / state validation 的 authoritative source；各 agent 的 result schema 與 semantic criteria 由 role prompts 定義；`SKILL.md` 保留 operator-level contract。README 提供整體概覽與操作入口，不逐條複製所有 internal validation。

## 測試這個 Repository

執行完整 test suite 與 compile check：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q run_codex.py tests/smoke_long_running_invocation.py
```

長時間 smoke harness 是 opt-in，使用前必須先建立合法的 pending Testing handoff；詳見 `tests/smoke_long_running_invocation.py`。

真實 end-to-end smoke test 應至少覆蓋：

- Task Review -> Coordinator iterative loop；
- no-change `COMPLETED` path；
- Testing RED mechanical verification；
- Testing test-fix allowlist + passing-command verification；
- ACCEPT -> BRIDGE -> DISPATCH handoff lifecycle；
- Draft PR 在第一個真實 implementation commit 後建立；
- Review -> Coordinator rework；
- specialist timeout / invalid result recovery；
- user decision pause；
- reviewed HEAD + PR description merge certification；
- final user merge gate；
- Issue / PR 中可見的 verified handoff / failure traces。

## 設計邊界

這仍是一個刻意保持小而可推理的 MVP。目前設計**不做**：

- 多個 agent 對同一個 worktree parallel execution；
- specialist-to-specialist direct routing；
- specialist agent 自行決定何時找使用者；
- automatic merge；
- generic agent adapters；
- webhooks 或 workflow database；
- exactly-once audit / handoff history infrastructure；
- 把 raw git / GitHub capability 完全隔離進 Executor sandbox。

這些限制讓 workflow 先維持 sequential、fail-closed、容易觀察與測試，再按真實需要擴張。
