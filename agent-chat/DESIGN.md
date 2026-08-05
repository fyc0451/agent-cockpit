# agent-chat · 团队 Agent 公共频道（设计文档 · 方案 Y）

> 状态：设计稿 v2（按 HazyValley 复核 + 用户决策修订）
> 目标读者：agent-cockpit 团队（人 + 各开发者 agent）
> 关联：`docs/team-collaboration-design.md`（整体产品设计）、ADR（团队频道协调边界，supersede/修订 reliable-mail-consumption）
> 复用：`agent-mail-tools`（mail-send / mail-recv / am-register）作为 Agent 接入层

## 1. 背景与目标

团队全员使用 AI coding agent，分布在各自本机。需求：本地照旧用各自 cockpit；服务器提供**按项目公共频道**，跨机交流走频道；服务器提供**身份目录 + 在线状态 + 简单 Web 看板**，便于 `@`；Agent 侧**CLI 调用不变**（mail-send / mail-recv / am-register 原样使用）。

非目标：本地 agent 互发；多团队 RBAC/审计（beta 后）；跨机终端接管；团队共享文件；herdr 任务调度。

## 2. 关键架构决策（方案 Y，已确认）

**Hub 保持哑总线；协调权威留在本机 sidecar；服务器只扩展"频道 fanout + 身份目录 + 在线 + @ 持久投递"。**

- 协调权威（claim/complete/checkpoint 状态机）**继续由本机 `coordination.py` sidecar 承担**，不迁移到 Hub——保住刚落地的可靠消费语义（租约、supersede 门控、trusted metadata、checkpoint-verify-resume），避免在外部组件内重写 1000+ 行状态机。
- Hub 作为**外部开源组件**（mcp_agent_mail）部署到团队服务器并轻扩展，只增加频道/目录/在线/投递相关能力；其 send/fetch/ack 原有语义不变。
- **@ 定向消息 = durable 投递**：服务器为每个 agent 维护持久收件箱，离线期间消息积压，agent 上线 `mail-recv` 补收——"上线即补收，不无故没下文"。
- 频道消息 = 持久历史 + per-agent 读游标；agent 上线拉"自游标以来未读"，不做全量流。
- 在线状态 = 被动心跳（mail 工具打点 `last_active_ts`），绝不反向唤醒 agent。

## 3. 总体架构

```
┌───────────── 开发者机器 A（codex-main）─────────────┐
│  cockpit UI / 终端 / 看板（本地照旧）                │
│  agent: mail-send / mail-recv（CLI 不变）           │
│  本机 coordination sidecar = claim/complete 权威     │
│  client.env.hub = http://<服务器>:8765              │
└──────┬──────────────────────────────────────────────┘
       │ JSON-RPC / Bearer token（send/fetch/ack）
┌──────▼──────────────────────────────────────────────┐
│          团队服务器（内网）                           │
│ ① Hub（外部开源 mcp_agent_mail，轻扩展）             │
│     · agents 表 = 团队通讯录（花名/program/所属项目） │
│     · 频道 fanout：channel:<项目slug>                │
│     · @ durable 收件箱（每 agent 持久队列）           │
│     · per-agent token + 审计日志                     │
│ ② Web 看板（消息流/成员/发言/@ 补全）                │
└──────▲──────────────────────────────────────────────┘
       │ client.env.hub = http://<服务器>:8765
┌──────┴───────────── 开发者机器 B（opencode-main）────┐
│  同 A：cockpit UI 本地，协调权威本地，mail 走服务器  │
└─────────────────────────────────────────────────────┘
```

## 4. 组件设计

### 4.1 ① 共享 Hub（M1，分两步）

**M1a（低风险，先验网络/token）**
- Hub 监听内网 + Bearer token；各机 `client.env.hub` 指向服务器；
- 验证跨机 send/fetch/ack 全链路（保持 Hub 原有工具面）。

**M1b（扩展，高风险点后置）**
- 服务器为 Hub 增加：频道 fanout、身份目录、@ durable 收件箱、per-agent token、审计；
- **不迁移** claim/complete/checkpoint 权威（本地 sidecar 保持）。
- 权威协调工具面：若需跨机统一某状态，走"服务器投递 + 本地权威消费"的解耦，不在 Hub 内重建状态机。

### 4.2 公共频道（M2，按项目）

- 每项目一个频道收件人 `channel:<项目slug>`；agent 默认归属注册项目，可额外订阅其他项目（订阅表存服务器）。
- 发言 = `mail-send --to channel:<项目slug>`；服务器 fanout 到频道订阅者 + 持久化频道历史。
- 收频道消息 = `mail-recv --channel <项目slug>`：
  - **默认视图：@ 提及自己的消息 + 自读游标以来的未读**（per-agent 游标，避免全量重读）；
  - 可选 `--full` 拉全量历史（显式使用）。
- **@ 解析（与频道读位解耦）**：消息中 `@花名` 无论是否同项目，一律生成一条**定向到被 @ agent 的 durable receipt**（写入其服务器收件箱），与频道广播无关——跨项目 @ 因此天然可达，不依赖订阅。
- 离线投递语义：频道广播是"黑板"（补拉未读）；@ 定向消息是"信件"（durable 收件箱，上线补收）。action-intent 的 @ 必须持久投递，不受在线状态门控。

### 4.3 身份目录与在线状态（M3）

- **目录**：Hub `agents` 表（花名/program/所属项目/注册时间）。
- **花名唯一性**：团队模式要求花名全局唯一。现状身份按项目路径分桶（同一 agent 在不同项目路径花名不同）。**迁移方案**：团队注册统一走服务器 `am-register`，花名以注册时为准 + 冲突检测；存量身份按"物理 agent 绑定一个团队花名"迁移（M1b 范围，逐步收敛）。
- **在线**：被动心跳——mail 工具每次调用顺带上报 `last_active_ts`；阈值需对照真实 poll 间隔校准（若 poll 本身 >3min，阈值放宽到如 2×poll 间隔）。在线纯展示，不做门控，不反向唤醒 agent。

### 4.4 ② Web 看板（M3）

- 频道消息流（按项目切换，游标增量拉取）；成员栏（花名/program/在线/最后活跃，在线优先）；发言框 `@` 自动补全（目录 + 在线优先）；人发言 = `human:<昵称>`（`trusted_user`，见安全）。
- 看板只做查看/发言，不做完整聊天室功能（beta 范围）。

## 5. 数据与权威边界

- **本地 coordination.sqlite3（本机）**：claim/complete/checkpoint 权威，继续持有 receipts/runs/participants 状态——可靠消费语义全部保留。
- **服务器 Hub（团队）**：agents 通讯录、频道历史、频道订阅、per-agent @ durable 收件箱、读游标、审计日志。
- **送达 vs 认领**：送达（durable 收件箱/频道历史）在服务器；认领/完成（处理状态）在本地。两者解耦，避免双重 claim 权威。
- **迁移/dual-run**：P0 阶段个人模式本地库仍是权威；M1b 团队试点用新项目/新频道起步，不迁移存量在途消息；存量数据迁移方案试点后再定。

## 6. 消息流时序

```
codex-main 请 opencode-main 复核（跨项目频道）
  codex-main: mail-send --to channel:agent-cockpit "@opencode-main 请看 PR #xxx"
  服务器: 写频道历史 + fanout(channel:agent-cockpit 订阅者)
           + 写 durable 收件箱(agent_cockpit_opencode, intent=action)
  opencode-main 上线: mail-recv --channel agent-cockpit（提及+未读）
           → 读到 @ → claim(本地权威) → 处理
  opencode-main: mail-send --to channel:agent-cockpit "结论: 可合入"
  服务器: 频道历史 + fanout；codex-main 及 Web 看板可见
  （若 codex-main 离线，fanout 不丢：上线后拉频道未读 / @ 它的收件箱补收）
```

## 7. 安全

- 内网 + token。**措辞修正**：服务器持有并校验共享/每-agent token（校验必需），但**不存 agent 私钥/凭据**。
- **per-agent token**：建议 beta 就发 per-agent token（存 agents 表），支持归因/吊销，避免"一 token 陷全家"；单一共享 token 只作 beta 过渡。
- **sender 可信边界**：共享 token 下 sender 是"断言"的；beta 以"内网内断言即信任"为已知边界，per-agent token 落地后按 token 归属绑定 sender。
- **human 可信**：`human:<昵称>` 以 `trusted_user` 进总线意味着可发 stop/redirect——必须绑定 per-agent token/登录态，不得仅凭共享 token 冒认 human。ADR"只有 user/lead 可 stop/redirect"的语义在共享 Hub 下据此保持。
- **审计**：保留/扩展 Hub 审计日志；action-intent 的 @ 与 stop/redirect 全量记录。
- **只读消息边界**：共享 Hub 的所有消息与 delivery 响应均视为不可信数据，只能作为经过字段白名单、长度限制和 HTML 转义的 DTO 展示。Hub 内容不能触发 shell/文件/终端/任务状态变更，也不能指定 Cockpit 下一步调用的工具。
- **显式授权**：发送、转为本地任务等动作必须由已认证用户在 Cockpit 明确发起；收到 `@` 只产生持久可见状态，不自动唤醒或向 Agent pane 注入 prompt。loopback 个人 Hub 可保留本地可靠消费通知，共享 Hub 不进入该链路。

## 8. 里程碑（修订版）

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 个人模式回归 | 全量测试通过；`hub=local` 时本地 coordination sidecar 单机工作不变 |
| M1a | Hub 内网可达 + 跨机 send/fetch/ack | 两机 agent 跨机基础通信全链路成功 |
| M1b | Hub 扩展（频道/目录/@ 收件箱/per-agent token/审计）；权威仍本地 | 服务器扩展可用；协调测试移植/扩展到服务器投递路径有回归保护 |
| M2 | 同项目公共频道 + 读游标 + @ durable 投递 | 同项目 agent 跨机频道发言/收听/离线补收成功 |
| M3 | 身份目录 + 在线 + Web 看板 + 跨项目 @ | 目录/在线可见、跨项目 @ 可达、人+agent 同场看板 |
| M4 | 团队试点 | codex-main ↔ opencode-main（+外部机）跨机互 @ 闭环 |
| M5 | 可选：多人登录 / per-agent token 收尾 / 待办汇入频道提及 | 试点后按真实痛点决定 |

- 协调相关测试随权威边界明确后移植到服务器投递路径（可靠性保证有回归保护）。
- run 跨机聚合**推迟**（等"编辑任务说明+持久化"落地，避免 garbage-in/garbage-out）。

## 9. 待确认开放点

1. 服务器 Web 看板独立服务还是挂在 cockpit 下（建议独立轻量服务）；
2. token 分发：per-agent token 的生成/下发流程（beta 可先共享 token 过渡）；
3. 存量身份迁移（花名唯一化）的节奏与范围；
4. **收件箱合并语义（进 M1b 前明确）**：方案 Y 下 agent 消息落在三处——现有直连 inbox、跨项目 @ durable 收件箱、订阅的频道历史。"CLI 调用不变"要成立，`mail-recv --unread` 默认应把 @ durable 收件箱与直连 inbox 合并进同一 `fetch_inbox` 视图；否则 agent 轮询行为要改。建议合并，需在实施时定。
5. **per-agent token 与 human stop/redirect 时序（进 M3 前明确）**：若 M1a 先用共享 token 跑、M3 看板又允许人发言，[共享 token, per-agent token 落地] 窗口内 human 冒认不可防。建议：人发言/stop-redirect 随 per-agent token 一起开，或看板侧先禁用 stop/redirect 到 per-agent token 就位。
6. **花名迁移的物理 agent 边界（进 M1b 前明确）**："物理 agent"是 (program) 还是 (program, instance)？worktree 身份（如 claude-main 在运行仓=HazyValley、worktree=RubyOwl）是否纳入统一迁移，还是只收敛主实例——需定清，避免迁移完 worktree 仍各注册各的。
