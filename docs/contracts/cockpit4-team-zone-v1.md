# Cockpit 4.0 · 第一刀合同（团队区）

> 分支：`feature/cockpit-4.0-team-zone`
> 基线：`aa0f575`（origin/main）
> 工作树：`/home/fyc/github/agent-cockpit-worktrees/cockpit-4.0`
> 主仓 `main` 工作区有别人的未提交改动，**禁止**在 `/home/fyc/github/agent-cockpit` 写 4.0。

## 已拍板

- 多 topic：一个 TeamProject 可以有多个 topic。第一刀 UI 先列出已加入的 topic，不让成员自建无限 topic。
- 「交给 leader」：只发生在团队区；点选，也可设默认。走团队通道，**不写**本机 `chat-ledger.sqlite3`。
- 一人多机：同一 Human + Hub + topic 同时只允许一个 active Session；后上线踢前一台。第一刀至少把冲突打成 409，踢人可跟绑定 API。
- **两本账彻底隔离（A）**：团队消息只待在团队区。没有「抄进本机群」。远程 `@` ≠ 执行，默认不 `pane_send`。

## 第一刀范围

未配 Team Hub 的人，界面与 3.0 完全一样。

1. 设置增加「团队」页：填 Team Hub + Human issuer（已有 `/api/agent-mail/config`）。不配就不出现团队区。
2. 侧栏工作区下面加「团队」区：登录团队账号 → 列出已加入 topic → 绑定**已有**本机 Session（唯一 leader 对外）。Hub 不收本机路径。
3. 团队时间线走独立账本 `team-messages.json`（store `team_messages`）。禁止把 Hub 历史写入 `chat-ledger.sqlite3`。
4. 团队区发消息 / 「交给 leader」只走 `/api/team*` 与团队账本。本机瀑布流、未读、打断/排队继续不动。

## 明确不做

- 不启 18790，不打开 native V2。
- 不把 4.0 合进 `main`，除非 Boss 再下令。
- 不改主仓脏文件（AgentMail 状态条那批）。
- 不派 FoggyBasin。
- 不做跨机终端、团队共享文件、远程调度 herdr。

## 文件边界

| 切片 | 主要文件 | 谁 |
| --- | --- | --- |
| 合同 + 隔离账本 | `docs/contracts/cockpit4-team-zone-v1.md`、`agent_cockpit/team_ledger.py`、store 注册 | BrownDesert |
| 设置「团队」页 | `web/pages/SettingsPage.tsx`、`web/api/*`、`web/test/settings-page.test.tsx`、`web/app/routes.ts` | BlueElk |
| 侧栏团队区 + 绑定 | `web/features/group-chat/SessionSidebar.tsx`、`GroupChatPage.tsx`、新 `web/features/team/` | GrayFalcon |
| 隔离测试 + 交给 leader 不进本机账 | `tests/test_team_ledger.py` 增补、团队 API 不调用 `chat_ledger.append_message` | OrangeGlacier |

## 验收（第一刀）

- 未配 Hub：设置没有团队区入口以外的副作用；群聊侧栏只有工作区。
- 配了 Hub：侧栏出现团队区；绑定本机 Session 成功后可在团队区发一条、看见一条。
- 团队消息文件是 `~/dashboard-data/team-messages.json`；`chat-ledger.sqlite3` 字节不因团队收发而增加。
- 「交给 leader」不在本机群多出气泡，也不默认 `pane_send`。
- 侧栏 topic 来自 Hub `/api/team/projects`（已加入），不是只从本机绑定抠。
- 绑定冲突 HTTP 409；确认后 `replace=true` 改绑。
- 已登录管理员可在侧栏「新建 topic」：只填名字，POST `/api/team/projects`，不选本机目录。
