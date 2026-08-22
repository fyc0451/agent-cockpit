# Agent Cockpit 最终优化方案 · 完整核对与实施路线

## 结论先行

两份报告对“能力已经存在，但事实源、接线和门禁不完整”的诊断基本一致，这部分保留；它们都基于 `bce87c9 + 未提交工作区`，因此优先级必须用当前 `main@5101599` 修正。3.0 定向消息栈、Leader/harvest/identity、聊天 read-model 不变式和 fast workflow 已经落地，不再列为缺口。

最终方案不是照报告评分开一轮 Task/UI 大重构，而是按以下边界推进：

1. 先恢复真正可执行的质量门禁。
2. 3.0 默认使用 A：共享目录 + 明确单写者；B：隔离 worktree + review/apply 只用于并行或高风险任务。
3. 聊天 ledger 迁移和 Task/Attempt 纵切分成两个批次，不把 SQLite、router 大拆、完整任务 UI 混在一起。
4. 审批、事件、Provider 和 router 只随已验证纵切增量收敛。

## 输入

- `cockpit-inbox/1787385800547-b195d90c-agent-cockpit-架构建议实现度核对报告-20260822.html`
- `cockpit-inbox/1787386595042-8fe67b5d-agent-cockpit-稳定性与多Agent开发调整分析-20260822.html`
- Git commit `510159915bb270f88448aa5c668ce2eb3d31a811`
- GitHub Actions run `32565271153`

## 当前事实核对

- 本地 required backend lane：`483 passed in 9.37s`。
- Web：`26 files / 220 tests passed`；production build 通过。
- 推送后的 `Fast required` workflow 被触发，但 GitHub 标注账户近期付款失败或 spending limit 不足，job 未启动；这不是测试失败，也不是成功运行。
- 聊天账本已迁到 `chat-ledger.sqlite3`：SQLite 是唯一写入与就绪检查权威，旧三份 JSON 仅在首次初始化时作为只读迁移输入。
- `agent_cockpit/scheduler_projection.py` 仍返回 `readonly_projection_only`。
- `agent_cockpit/workspace_dispatch_service.py` 仍传 `approval_required=False`。
- `web/features/group-chat/AgentInteractModal.tsx` 仍有 `send('y Enter', 'keys')`。

## 模块规模采集

在 `main@5101599` 工作树执行：

```text
wc -l agent_cockpit/server.py agent_cockpit/herdr_client.py web/features/group-chat/GroupChatPage.tsx web/features/group-chat/model.ts
```

结果保存于同目录 `ac-opt-size.csv`。

## 两份报告的交叉核对

### CI 与回归门禁

两份报告都把 CI 缺口列为优先事项，这个方向正确。当前本地 backend required lane、Web 全量测试和 production build 已通过，workflow 也已推送；但远端 job 因 GitHub 账户支付失败或 spending limit 不足而没有启动。因此状态只能写“工作流已配置、本地基线通过、远端自动门禁不可用”，不能写“CI 已完成”。

### 聊天 read-model

稳定性报告主张聊天单一写入链和稳定 read-model。当前群聊读取仍以 session、mail-status、Hub/SSE 和会话迟到/滚动不变式为核心，相关回归门禁已落地。后续改聊天持久层时必须保持 HTTP/SSE 与前端 read-model 契约，不把 UI 状态反向变成权威事实源。

### 多 Agent 写入模型

worktree 能提供良好隔离，但日常群聊 Agent 仍共享目录，全面迁移成本和产品风险过高。推荐 A 为默认：系统对同一工作区登记唯一 writer，其他 Agent 只读、分析或通过定向消息协作。只有并行写入、高风险改动或明确需要隔离评审时才进入 B，由系统创建 managed worktree，产出结构化 Handoff/Review 后 apply。

### Task、Handoff 与 Review

两份报告指出 Task/Attempt/Handoff/Review 尚未形成统一闭环，这仍成立。仓库已有 tasks、work_items、assignments 等概念和 apply 能力，不应另起第二套 Mission Board 或完整 Task DAG。先写 ADR 固定权威模型、状态机、`allowed_paths` 和失败语义，再选择一个 managed Codex 流程做端到端纵切。

### 大文件与 router

`server.py` 等文件体量确实扩大了 review 半径，但“文件大”不是立即大拆的充分条件。先用真实纵切确定稳定边界，再逐域提取；禁止为了降低 LOC 一次性迁移 131 个路由或重写运行时。

## 最终实施路线

### M0 · 已完成：3.0 定向消息收口

只包含定向消息栈、Leader/harvest/identity、read-model 不变式与 fast workflow。对应提交 `42176d2`、`5101599` 已推到 `origin/main`；本地 backend 483、Web 220 和 production build 通过。本批没有混入 SQLite ledger、Task/UI 或 router 大拆。

### M1 · 恢复真实质量信号

如果继续维持 private 仓库且不升级套餐，仓库内提供一条统一 fast 命令，并把“必须本地执行并附结果”写入合并清单；如果修复 GitHub payment/spending limit，则等 `Fast required / Python + web` 首次真实绿灯。验收标准是开发者和 Agent 都能用一个入口重跑相同的 backend/Web/build 基线。

### M2 · 已完成：聊天 ledger 单一权威

本批单独把 JSON 截断账本迁到 SQLite，保持现有 HTTP/SSE 契约与 read-model 不变。SQLite 不再截断 500 条以上历史；首次访问在单一事务中导入旧 workspace/thread/message JSON，成功后不再重读，失败则回滚 SQLite 且不改旧文件，可安全重试。未知来源的工作区根层 `cockpit.db` 未删除、未复用。

### M3 · 一个 managed worktree 纵切

先完成 Task/Attempt ADR，明确状态机、`allowed_paths`、owner、失败/取消和 apply 语义；然后只接一类 Codex 任务：dispatch → managed worktree → structured Handoff/Review → apply。验收要求越界文件阻断、失败保留证据、apply 后可追溯，并且不影响普通 A 模式群聊。

### M4 · 增量治理

审批、event、Provider 和 router 只在 M3 暴露出明确边界时逐步接入。每一域独立提交、独立回归；不做一次性 router 大拆、全自动 scheduler 或完整第二套编排 UI。

## 明确不做

- 不按报告完成度百分比直接排期。
- 不先建 Mission Board、Task DAG、Review Inbox 或第二套任务 UI。
- 不把 C 决策推断成 GitHub Actions 已可用，也不把它推断成后续 push 授权。
- 不把所有聊天 Agent 一次性迁到 worktree。
- 不在同一个提交里混入 ledger SQLite、router 拆分和 Task/UI 重构。

## 需要 Boss 确认的两项

1. 是否接受 A 为 3.0 默认写入模型、B 为受控能力。
2. M1 选择“本地统一 fast 合并门禁”还是修复 GitHub Actions 账户额度后恢复远端绿灯。

## 方法限制

- 这是静态架构核对与 focused 验证，不是长期负载、断网恢复或破坏性测试。
- 完成度百分比未进入最终排序；最终顺序按已落地事实、风险、依赖和可独立验收性制定。
- 旧 TypeScript 产品线与当前 Python/FastAPI + React 3.0 分开判断。
