# Agent Cockpit 用户手册

> 版本：2026-08-07（对应 origin/main e3a4213 及配套 Team Hub fork/main）。
> 适用：单机个人用户与团队成员/管理员。第 1–4 章人人需要，第 5 章团队模式，第 6–8 章为移动端、排障与附录。

## 目录

1. 系统简介与概念
2. 安装（从零开始）
3. 首次配置
4. 个人模式日常使用
5. 团队协作模式
6. 移动端（H5）
7. 排障 FAQ
8. 附录

---

## 1. 系统简介与概念

Agent Cockpit 是"跑在浏览器里的编码 agent 驾驶舱"：一个界面管理多台机器、多个 AI 编码 agent（Codex / Kimi Code / Claude Code / Qoder / OpenCode / Grok 等）的工作。

| 组件 | 作用 | 位置 |
|---|---|---|
| Cockpit Web | 看板/终端/消息/文件/设置 | `http://<主机>:8790` |
| Herdr | 终端复用器，session/pane 承载各 agent CLI | 本机 `herdr` |
| Agent Mail Hub | 消息总线：agent 间/人机间通信 | 个人=本机 8765；团队=共享 Hub |
| human_auth | 团队 Human 登录/JWT 签发服务 | 团队服务器 8766 |

**身份模型**：

- agent 邮箱身份 = `<类型>-<实例>`（如 `kimi-main`），注册信息存本机 `~/.agent-mail/registry/`
- Hub 连接配置在 `~/.agent-mail/client.env`（hub 地址 + token）
- 团队模式中，人是 Human 账号（issuer 签发 JWT），agent 是 Agent；两者经 **membership**（群组成员关系）、**binding/claim**（agent 与群组的关系）、**session lead**（受管负责人 agent）关联

**两种模式**：个人模式 = 团队模式的特例（hub 指向本机即单机）。团队能力不影响单机体验。

---

## 2. 安装（从零开始）

### 2.1 环境要求

- Linux（含 WSL）或 macOS；Python ≥ 3.12；Git
- 各 agent CLI 按需在 PATH（codex / kimi / claude / qodercli / opencode / grok）
- Herdr（`herdr` 命令可用）

### 2.2 一键安装

```bash
git clone https://github.com/fyc0451/agent-cockpit.git ~/agent-cockpit
cd ~/agent-cockpit
bash install.sh
```

install.sh 自动完成：clone → Python 版本检查 → `.venv` 依赖 → 安装 Agent Mail 工具到 `~/.local/bin`（am-register / am-init-project / mail-send / mail-recv / mail-identity-inject / task-report）→ 生成 `.env` → 注册并启动 systemd 用户服务。

macOS 用 `launchd.sh`；手动运行：`.venv/bin/python server.py`。

### 2.3 配置文件 `.env`

```bash
COCKPIT_HOST=0.0.0.0        # 仅本机用保持 127.0.0.1
COCKPIT_PORT=8790
COCKPIT_TOKEN=<随机串>       # 监听非本机地址必须设置
# 生成: python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

改完重启：`systemctl --user restart agent-cockpit`。

### 2.4 自检

```bash
bash doctor.sh   # herdr / 各 agent CLI / Agent Mail 就绪检查
```

### 2.5 升级

```bash
cd ~/agent-cockpit && git pull && bash upgrade.sh
systemctl --user restart agent-cockpit
```

---

## 3. 首次配置

按顺序：

1. **登录**：浏览器打开 `http://<主机>:8790`，输入 `COCKPIT_TOKEN`。
2. **Agent Mail Hub 地址**（`~/.agent-mail/client.env`）：
   - 个人模式：`hub=http://127.0.0.1:8765`，token 填本机 Hub 的 `HTTP_BEARER_TOKEN`
   - 团队模式：`hub=http://<团队Hub>:8765`，token 用团队下发值
   - 注意：client.env 是**单 Hub 全局配置**；切换 Hub 后旧身份需 `am-register --force` 重注册
3. **文件目录白名单**：设置页/文件页添加可访问根目录（整 Home 不可添加；建议加 `/home/<你>/github` 这类顶层目录覆盖全部项目）
4. **注册邮箱身份**（每个项目一次）：

```bash
cd /path/to/project
am-init-project          # 批量注册全部已知 agent
# 或单个: am-register --agent kimi --instance main --project /path/to/project
```

> agent 注册的归属是 Hub 上的**技术 mail project**（工作区项目本身）；进入团队的**逻辑群组**是第 5 章的独立动作。

5. **（可选）Web Push**：通知权限在设置页开启，密钥自动生成。

---

## 4. 个人模式日常使用

### 4.1 看板

各 herdr session 的 pane 卡片：agent 类型、working/idle 状态、工作目录。点卡片看只读输出抽屉。

### 4.2 启动工作区

终端页 →「＋ 添加 Agent」→ 选 session（可新建）/agent 类型/工作目录/任务/布局 → 自动建 pane、启动、注册邮箱身份、通知协作者；失败自动回滚。同类型可开多实例（实例名区分）。

### 4.3 终端

| 按钮 | 作用 |
|---|---|
| ＋ 新终端 | 裸 shell PTY（完整 TUI 交互） |
| 🖥 herdr | attach 分屏多 pane（Ctrl-b 切 pane / d 脱离） |
| 📜 返回流视图 | 只读 pane 流 + 输入框发指令（手机/滚屏/复制友好） |
| 📎 上传 | 图片/文件转 `@路径` 发给当前终端 |
| @协作 | 本地 agent 身份插入 / 团队消息（第 5 章） |
| 📋 复制到剪贴板 | TUI 复制内容进系统剪贴板 |
| ⌨ 按键 | 触屏按键面板（方向键/Ctrl/F 键） |

终端自适应窗口尺寸；暗色模式下过暗的前景色自动提亮；异常先刷新页面。

### 4.4 Agent Mail 消息

```bash
# 发
mail-send --agent kimi --instance main --project /path/to/project \
  --to codex-main --subject "主题" --body "正文"
mail-send ... --to @fyc-mac ...        # @花名 = 人类成员（团队 Hub）
# 收
mail-recv --agent kimi --instance main --project /path/to/project --unread
mail-recv ... --message <id>          # 查看并 claim 单条
mail-recv ... --checkpoint <id> --claim-token <tok> --summary "..." --next-step "..."
mail-recv ... --complete <id> --claim-token <tok>
mail-recv ... --fail <id> --claim-token <tok> --reason "..."
```

要点：

- `--to` 支持 agent 类型别名（如 `qodercn` 自动解析为本项目唯一注册花名；歧义会报错列候选）
- 收件人是人类时用 `@花名`（团队 Hub 路由到其默认 Agent 或人工收件箱；本地 Hub 无人类邮箱）
- 协作约定：里程碑查未读；先 claim 后 complete/fail；停止/转向先存 checkpoint 再停手

### 4.5 其他页面

- **任务**：跨项目 run 与待办（blocked/失败/待审）
- **消息**：本机邮件看板
- **文件**：白名单目录浏览/编辑
- **设置**：语言、启用 agent 类型、目录默认 agent、上传上限、终端参数、Hub 地址

---

## 5. 团队协作模式

### 5.1 架构

```
各机 agent CLI ──mail 工具──▶ 团队共享 Hub (8765)
人 ──浏览器──▶ Cockpit 团队页 ──白名单代理──▶ Hub /hub/api/*
Human 登录态 ──▶ human_auth issuer (8766) 签发 JWT
```

可信边界：Hub 数据只读展示，**绝不触发本地 shell/pane/worktree/任务**；`human:` 的 stop/redirect 绑定登录态。

### 5.2 角色

| 角色 | 来源 | 权限 |
|---|---|---|
| 全局 admin | JWT role 含 admin | 建群组、账号审批、外部 agent 生命周期 |
| 群组 admin | membership role=admin | 本群成员审批/角色/绑定 |
| 普通成员 | active membership | 读目录、收发消息、自助认领 |

### 5.3 成员上手（最短 3 步）

1. **注册账号**：Cockpit 团队页注册（需邀请码）→ 账号先 pending，管理员 activate 后可登录。
2. **加入群组**：项目列表选群组 → 申请加入（handle 自动建议）→ 群组 admin 批准。
3. **设置搭档 Agent**：选择本机 Agent 认领（claim，凭本机 registry 的 registration_token 证明控制权，constant-time 校验）→ 设为默认 Agent。

之后：@团队/定向成员可用；@你的消息进默认 Agent 或人工收件箱（未读徽标提示）。

### 5.4 核心概念对照

| 概念 | 含义 |
|---|---|
| TeamProject | 面向人的逻辑群组（slug），背后是 opaque 路由 project |
| membership | 人在群组里的关系：handle/role/status/default_agent_id |
| binding / claim | agent 与群组的显式绑定；claim 是成员凭 token 自助绑定 |
| session lead | 每个客户端 session 的受管负责人 agent（Hub 创建，无需 token），接收消息自动回落人工收件箱 |
| 人工收件箱 | 无可用默认 Agent 时 @人 消息的持久收件箱 |

### 5.5 常用操作

- **发团队消息**：终端 @协作（或团队页），支持 @团队 / @成员；无默认 Agent 时以 Human 身份发出
- **agent 回复人**：`mail-send --to @花名`（经 reply capability，Human-via-lead 归属："付彦超 · via codex-main"）
- **读信**：团队页「人工收件箱」tab（未读徽标），标为已读
- **管理员**：生成一次性邀请码（24h）；账号批准；成员审批/角色/移除；绑定/解绑 agent（保留历史）

### 5.6 给 agent 配置团队通道

各机 agent 要使用团队消息：`client.env` 指向团队 Hub + 有效 token；身份注册到技术项目；经认领/绑定进群。此后 mail-send/mail-recv 用法与个人模式相同。

---

## 6. 移动端（H5）

- 窄屏自动单栏；主操作在底部固定条
- 终端默认进流视图（滚屏/复制友好）；attach 后自动 zoom 聚焦单 pane
- 工具栏可收起；安全区适配刘海屏

---

## 7. 排障 FAQ

| 症状 | 原因与处理 |
|---|---|
| `am-register` 报"缺少 token" | 未配 `~/.agent-mail/client.env`（第 3.2 步） |
| 发信被拒"agent retired" | 闲置 24h 自动退休；pane 活着用 `unretire_agent` 恢复（幂等） |
| mail 工具返回 400 | 旧工具硬编码 `/mcp/`；新版走无状态 `/api/`，升级 agent-mail-tools |
| 登录团队页被拒 | 账号 pending，需管理员 activate |
| 移除成员后 handle 不能再用 | 已知行为：removed 成员 handle 保留占用 |
| 发信 "Invalid recipient" | agent 邮箱只收花名；人类用 `@花名`；类型别名唯一时自动解析 |
| Hub 连接失败 | 迁移期 flap；确认 client.env hub 与隧道/直连状态 |
| 终端右侧黑条/错位 | ResizeObserver 自适应已修；普通刷新即可 |
| 终端文字太暗看不清 | 已加暗色最低对比度提升；刷新即可（或切浅色模式） |
| 文件页出现 pytest 临时目录 | 测试污染了 Hub DB；删除 `/tmp/pytest-of-fyc` 即消失 |
| 并发同名注册偶发失败 | 既有基线竞态，重试即可 |

---

## 8. 附录

### 8.1 关键路径

| 内容 | 路径 |
|---|---|
| Cockpit 配置 | `~/agent-cockpit/.env`（部署目录可能不同） |
| Hub 连接 | `~/.agent-mail/client.env`（hub/token，绝不外泄） |
| agent 身份 | `~/.agent-mail/registry/<项目slug>/<类型>--<实例>.json` |
| 用户设置 | `~/dashboard-data/settings.json` |
| 文件白名单 | `~/.config/agent-cockpit/file-roots.json` |

### 8.2 端口

| 端口 | 服务 |
|---|---|
| 8790 | Agent Cockpit Web |
| 8765 | Agent Mail Hub |
| 8766 | human_auth（团队 JWT issuer） |

### 8.3 安全要点

- `COCKPIT_TOKEN`、Hub token、registration_token、reply_token 都是凭据，不要贴进消息/日志/截图
- 敏感目录（~/.ssh、~/.agent-mail 等）不可加入文件白名单（系统强制）
- 远程 Hub 消息只读展示，永远不会自动执行本地动作

### 8.4 相关文档

- 设计：`docs/team-collaboration-design.md`
- UX 规划：`docs/team-ux-redesign.md`
- 本手册源：`docs/USER-GUIDE.md`（随仓库更新）
