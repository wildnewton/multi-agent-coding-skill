# Multi-Agent Coding Skill（多智能體編碼技能）

這是一個給 Hermes 使用的多智能體編碼 Skill，用來協調多個 Codex agent 執行 **tests-first** 的軟體開發流程，同時讓路由、驗證與合併決策保持明確、可檢查。

整個工作流分成四個專門角色：

- **Coordinator（協調者）** — 語義上的路由中心。負責理解需求、決定下一步、修改 production code，也是唯一可以決定把工作交給哪一個 specialist agent 的角色。
- **Task Review（任務審查）** — 在 implementation 前獨立驗證 task / requirement / acceptance criteria；每次都是全新的唯讀審查。
- **Testing（測試）** — 負責測試意圖與 RED state。可以修改 tests、fixtures 與 test-only helpers，但不能修改 production code。
- **Review（審查）** — 對目前的 GREEN state 進行一次全新的唯讀審查。它負責提出 findings，但不能修改檔案，也不能決定下一個 agent。

Hermes 位於這些角色之間，擔任機械式 dispatcher 與 verifier。它只執行 Coordinator 核准的轉移、檢查 repository / test evidence、維護 workflow state，並把已驗證的 handoff 記錄發布到 pull request。

## 為什麼需要這個專案

多智能體 coding workflow 很容易在「每個 agent 都能自行決定下一步」時變得脆弱。這個專案刻意把**語義路由集中在 Coordinator**，而把可重現、可機械驗證的檢查交給 Hermes。

目標是形成這樣的工作流：

1. 需求由中央 Coordinator 統一理解與分派；
2. 在 production implementation 之前，先由 Task Review 驗證 task，再由測試定義缺失的行為；
3. production change 只有在 GREEN state 通過機械驗證後，才進入 Review；
4. 驗證失敗時，把 evidence 返回 Coordinator，而不是由 Hermes 暗中猜下一步；
5. 每一次已驗證的 phase transition 都可以在 PR 中留下 audit trail；
6. merge 永遠由使用者明確決定。

## 架構

邏輯上的溝通關係：

```text
使用者 <-> Coordinator
            <-> Task Review
            <-> Testing
            <-> Review
```

實際 transport 永遠經過 Hermes：

```text
Coordinator -> Hermes -> Task Review -> Hermes -> Coordinator
Coordinator -> Hermes -> Testing -> Hermes -> Coordinator
Coordinator -> Hermes -> Review  -> Hermes -> Coordinator
```

Task Review、Testing 與 Review 不會直接互相路由。它們都必須把結果交回 Coordinator，再由 Coordinator 決定下一個語義步驟。

## 工作流程

目前的 MVP 刻意維持 sequential execution。

```text
使用者需求
    |
    v
Coordinator
    |
    | HANDOFF -> Task Review
    v
全新的 Task Review session
    |
    +--> CHANGES_REQUIRED -> Coordinator -> fresh Task Review
    |
    +--> TASK_REVIEW_CLEAN
    |
    | HANDOFF -> Testing
    v
Testing 建立 RED test intent
    |
    v
Hermes 驗證 RED
    |
    v
Coordinator 實作 / 修正 production code
    |
    v
Hermes 驗證已 commit 的 GREEN state
    |
    | HANDOFF -> Review
    v
全新的 Review session
    |
    v
Coordinator
    |
    +--> 修正 implementation
    +--> HANDOFF -> Testing，重做 / 補強測試
    +--> AWAIT_USER_DECISION
    +--> AWAIT_USER_MERGE
```

Review 回傳 `CHANGES_REQUIRED` **不代表流程自動停止，也不代表一定要回到 Testing**。Review 只把 evidence 交給 Coordinator；Coordinator 再判斷問題是 implementation、test coverage、requirements，或其他原因。

## 驗證 Gate

Hermes 不會只因為 agent 聲稱「成功」就相信結果。重要 transition 發生前，都必須通過 deterministic checks。

### Task Review gate

在 Testing 或 implementation 之前，Task Review 必須對目前 task 回傳 `TASK_REVIEW_CLEAN`；`CHANGES_REQUIRED` 會回到 Coordinator 修正 task，再送新的 Task Review。

### RED gate

在接受 Testing 的 `RED_COMPLETE` 之前，Hermes 會驗證：

- 回報的 RED commit 存在，而且是目前的 commit；
- changed files 只包含 tests、fixtures 或 test-only helpers；
- 指定的 targeted test command 確實因為缺失行為而失敗。

### GREEN gate

在呼叫 Review 之前，Hermes 會驗證：

- 回報的 GREEN commit 存在，而且等於目前的 `HEAD`；
- 沒有尚未 commit 的 tracked / staged changes；
- RED test intent 沒有被悄悄弱化；
- targeted test command 通過；
- 如果有 full test suite，必須通過；若無法執行 full suite，Coordinator 必須提供明確原因；
- 如果 repo 設有 CI，CI 必須通過。

### Merge gate

在詢問使用者是否 merge 之前，Hermes 會驗證：

- 目前 task 仍有有效的 `TASK_REVIEW_CLEAN` certification；
- Review 對 `reviewed_head` 回傳了 `REVIEW_CLEAN`；
- 目前 HEAD 仍然等於那個已 review 的 commit；
- Review 之後沒有新增 tracked 或 staged changes；
- 必要 tests / CI 仍然通過；
- PR description 與實際 implementation 和 test evidence 一致。

這個 workflow **永遠不會自動 merge**。

## Agent session policy

- **Coordinator** — 每個 workflow id 保留一個 persistent Codex session。
- **Task Review** — 每次都建立全新的 Codex session。
- **Testing** — 每個 workflow id 保留一個 persistent Codex session。
- **Review** — 每次都建立全新的 Codex session。

persistent session id 預設儲存在 `state/<workflow-id>.json`。Task Review 與 Review 刻意不保留 session，避免前一次 review 的上下文無意中影響下一次審查。

## Result contract

每次 agent invocation 結束時，都必須輸出**且只輸出一行** machine-readable result：

```text
HERMES_RESULT={...}
```

每個角色允許的 status 不同：

| Agent | 可接受 status |
| --- | --- |
| Coordinator | `HANDOFF`, `AWAIT_USER_DECISION`, `AWAIT_USER_MERGE`, `BLOCKED` |
| Task Review | `TASK_REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED` |
| Testing | `RED_COMPLETE`, `BLOCKED` |
| Review | `REVIEW_CLEAN`, `CHANGES_REQUIRED`, `BLOCKED` |

重要限制：

- 只有 Coordinator 可以回傳 `next_agent`。
- Coordinator 的 `HANDOFF` 只能指向 `task_review`、`testing` 或 `review`。
- HANDOFF 到 Review 時，必須包含 `commit`、`test_command`，以及 `full_test_command` / `full_test_unavailable_reason` 兩者之一，而且只能有一個。
- `AWAIT_USER_DECISION` 必須包含非空的 `question`。
- `AWAIT_USER_MERGE` 必須包含 `reviewed_head`。
- Task Review、Testing 與 Review 不得包含 `next_agent`。
- malformed、互相矛盾或與角色不相容的 result 會直接被拒絕，不會根據 prose 猜測其含義。

## 使用前條件

目標 coding repository 應存在於本機，而且 worktree 必須乾淨。工作流另外需要：

- Python 3；
- 已安裝並完成認證的 `codex` CLI；
- 已安裝並完成認證的 `gh` CLI，而且對目標 repository 有建立 PR 與留言的權限；
- 在開始修改 code 前，先在目標 coding repository 建立 dedicated feature branch。

Skill repository 本身不需要就是被修改的 repository。`run_codex.py --repo` 會把每個 agent 指向真正的 target coding repository。

## 執行 Agent

`run_codex.py` 是這個 workflow 使用的薄層 Codex CLI wrapper。

啟動或恢復 Coordinator：

```bash
python3 run_codex.py \
  --agent coordinator \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Implement issue #123. Acceptance criteria: ...'
```

只有當 Coordinator 回傳有效的 `HANDOFF -> task_review` 時，才呼叫 Task Review：

```bash
python3 run_codex.py \
  --agent task_review \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Review the exact task requested by Coordinator ...'
```

只有在 Task Review clean 且 Coordinator 回傳有效的 `HANDOFF -> testing` 時，才呼叫 Testing：

```bash
python3 run_codex.py \
  --agent testing \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Add the RED test requested by Coordinator ...'
```

只有在 Coordinator 請求 Review，而且 Hermes 已驗證 GREEN gate 之後，才呼叫 Review：

```bash
python3 run_codex.py \
  --agent review \
  --workflow issue-123 \
  --repo /path/to/target-repo \
  --task 'Review current HEAD against the acceptance criteria and RED evidence ...'
```

可選參數：

- `--state-file <path>` — 覆寫預設 workflow state file。
- `--prompt-dir <path>` — 覆寫內建的 `prompts/` 目錄。

這個 wrapper 會驗證不同 agent 的 result contract，並為 Coordinator 與 Testing 保存 Codex session。**它本身不做語義路由決策。**

## PR handoff audit trail

PR 建立後，Hermes 可以為每一次完成且已驗證的 handoff，發布一則新的 top-level PR conversation comment。

Audit trail 會區分：

- Task Review handoff 與 result；
- Testing handoff 與 RED verification；
- Coordinator 的 routing decision 與 GREEN evidence；
- Review result 與 reviewed HEAD；
- Coordinator 的 merge-ready decision。

Agent 不會自行發布 handoff comment。Hermes 會先驗證 machine result 與 repository evidence，再發布給人閱讀的 verified record。

## Repository 結構

```text
.
├── SKILL.md                 # Hermes workflow 與 routing 的完整規格
├── run_codex.py             # Codex session / state / result-contract wrapper
├── prompts/
│   ├── coordinator.md       # Coordinator 角色規格
│   ├── task_review.md       # Task Review 角色規格
│   ├── testing.md           # Testing 角色規格
│   └── review.md            # Review 角色規格
├── tests/                   # Runner / workflow regression tests
├── .github/workflows/       # 這個 Skill repository 的 CI
└── README.md
```

`SKILL.md` 是 authoritative workflow specification。README 的用途是提供整體概覽與 operator-oriented entry point，而不是逐條複製所有 routing rule。

## 測試這個 Repository

執行完整 test suite：

```bash
python3 -m unittest discover -s tests -v
```

若要做 end-to-end smoke test，可以選一個小型真實 coding issue，確認 workflow 能處理：

- Task Review -> Coordinator loop；
- Testing -> Coordinator loop；
- Review -> Coordinator loop；
- Coordinator -> Testing rework；
- 等待使用者決策的 pause；
- 最後的 user merge gate；
- PR 中可見的 verified handoff comments。

## 設計邊界

這仍是一個 MVP。目前設計刻意**不做**以下事情：

- 多個 agent 對同一個 worktree parallel execution；
- Testing -> Review 直接路由；
- Review -> Testing 直接路由；
- specialist agent 自行決定何時找使用者；
- automatic merge；
- generic agent adapters；
- webhooks 或 workflow database。

把這些限制明確保留下來，可以先讓 workflow 維持容易觀察、容易測試、容易推理，再考慮加入 concurrency 或更多 infrastructure。
