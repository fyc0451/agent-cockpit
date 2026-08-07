# Agent Cockpit 用户手册

> 版本：2026-08-07（Cockpit origin/main e3a4213；Team Hub 97a20eb）。
> 第 1–4 章人人需要；第 5 章团队模式；第 6–8 章移动端、排障与附录。

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

Agent Cockpit 是"跑在浏览器里的编码 agent 驾驶舱"：一个浏览器界面管理**本机**的多个 AI 编码 agent（Codex / Kimi Code / Claude Code / Qoder / OpenCode / Grok 等）。

| 组件 | 作用 | 位置 |
|---|---|---|
| Cockpit Web | 看板/终端/消息/文件/设置 | `http://<主机>:8790` |
| Herdr | 终端复用器，session/pane 承载各 agent CLI | 本机 `herdr` |
| 本地 Agent Mail Hub | agent 间消息（本机） | 本机 8765 |
| Team Hub（远程） | 团队消息/成员/群组 | 团队服务器 8765 |
| human_auth（远程） | 团队 Human 登录/JWT 签发 | 团队服务器 8766 |

**关键边界**：

- 每个 Cockpit 只管理**本机**的 Herdr/session/终端；跨机只有 Team 消息通信，**不远控其他机器**。
- **本地 Agent Mail 与远程 Team Hub 是两条独立链路**：`client.env` 只控制本地 Agent Mail；Team Hub / Human issuer 在设置页分别配置。本地 agent 永远保留本机 Hub；agent 回复人类经本机 Cockpit 的回环 reply 代理 + Session 绑定 capability 到远程 Team Hub。

**身份模型**：

- 本机 registry 身份文件 `~/.agent-mail/registry/<项目slug>/<类型>--<实例>.json`——文件名只是本地 selector；**真正的 Agent Mail 花名是注册时 Hub 返回并存在文件里的 `name`**（可能是 `RedHawk` 这类随机名，不保证是 `codex-main`）。
- 团队侧 Human 有自己的 mention_handle（如 `fyc-mac`）——那是**人类 handle，不是 agent 花名**，两者不要混用。

---

## 2. 安装（从零开始）

### 2.1 环境要求

- Linux（含 WSL）或 macOS；Python ≥ 3.12；Git
- 各 agent CLI 按需在 PATH；Herdr 可用

### 2.2 安装

```bash
git clone https://github.com/fyc0451/agent-cockpit.git ~/agent-cockpit
cd ~/agent-cockpit
bash install.sh
```

install.sh 会校验现有 checkout（只有 curl 直装方式才自动 clone），然后：Python ≥3.12 检查 → `.venv` 依赖 → 安装 Agent Mail 工具到 `~/.local/bin` → 生成 `.env` → 注册并启动服务（Linux systemd 用户服务；macOS 用 launchd.sh）。

### 2.3 配置文件 `.env`

```bash
COCKPIT_HOST=127.0.0.1      # 默认仅本机访问（推荐）
COCKPIT_PORT=8790
# 局域网/手机访问时才改为 0.0.0.0，且必须同时设置:
# COCKPIT_TOKEN=<随机串>     # python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

改完重启服务。

### 2.4 自检与升级

```bash
bash doctor.sh      # herdr / 各 agent CLI / Agent Mail 就绪检查
bash upgrade.sh     # 一键升级(拉代码+装依赖+重启服务,Linux/macOS 通用)
```

---

## 3. 首次配置

1. **登录**：浏览器打开 `http://<主机>:8790`（设置了 token 则用 token 登录）。
2. **本地 Agent Mail**：`~/.agent-mail/client.env` 配好本机 Hub（默认 `http://127.0.0.1:8765` + 本机 Hub token）。
3. **团队通道（可选）**：设置页分别配置 Team Hub 地址与 Human issuer——与本地 Hub 互不影响。
4. **文件目录白名单**：设置页/文件页添加可访问根目录（整 Home 不可添加；建议加 `~/github` 这类顶层目录）。
5. **注册邮箱身份**（每个项目一次）：

```bash
cd /path/to/project
am-init-project          # 批量注册全部已知 agent
# 或单个: am-register --agent kimi --instance main --project /path/to/project
```

> 重复运行是安全的：已有身份自动复用；身份被闲置退休（retired）时会持 registration token 自动恢复激活。

---

## 4. 个人模式日常使用

### 4.1 看板

各 herdr session 的 pane 卡片：agent 类型、working/idle 状态、工作目录。点卡片看只读输出抽屉。

### 4.2 启动工作区

终端页 →「＋ 添加 Agent」→ 选 session（可新建）/agent 类型/工作目录/任务/布局 → 自动建 pane、启动、注册邮箱身份、通知协作者；失败自动回滚。同类型可开多实例。

### 4.3 终端

| 按钮 | 作用 |
|---|---|
| ＋ 新终端 | 裸 shell PTY（完整 TUI 交互） |
| 🖥 herdr | attach 分屏多 pane（Ctrl-b 切 pane / d 脱离） |
| 📜 返回流视图 | 只读 pane 流 + 输入框发指令（手机/滚屏/复制友好） |
| 📎 上传 | 图片/文件转 `@路径` 发给当前终端 |
| @协作 | 本地 agent 身份插入 / 团队消息（第 5 章） |
| 📋 复制到剪贴板 | TUI 复制内容进系统剪贴板 |

终端自适应窗口尺寸；暗色模式下过暗前景色自动提亮；异常先刷新页面。

### 4.4 Agent Mail 消息

```bash
# 发(对方花名 = 其 registry 文件里的 name,不是文件名)
mail-send --agent kimi --instance main --project /path/to/project \
  --to <对方 Agent Mail 花名> --subject "主题" --body "正文"
# 回复人类成员(团队模式,见 5.4)
mail-send ... --to @fyc-mac --subject "..." --body "..."
# 收
mail-recv --agent kimi --instance main --project /path/to/project --unread
mail-recv ... --message <id>          # 查看并 claim 单条
mail-recv ... --checkpoint <id> --claim-token <tok> --summary "..." --next-step "..."
mail-recv ... --complete <id> --claim-token <tok>
mail-recv ... --fail <id> --claim-token <tok> --reason "..."
```

要点：

- `--to` 支持 agent 类型别名（如 `qodercn` 自动解析为本项目唯一注册花名；歧义报错并列候选）
- 协作约定：里程碑查未读；先 claim 后 complete/fail；停止/转向先存 checkpoint 再停手

### 4.5 其他页面

- **任务**：跨项目 run 与待办（blocked/失败/待审）
- **消息**：本机邮件看板
- **文件**：白名单目录浏览/编辑
- **设置**：语言、启用 agent 类型、目录默认 agent、上传上限、终端参数、本地 Hub 与 Team Hub/issuer 配置

---

## 5. 团队协作模式

### 5.1 架构与边界

```
本机 agent CLI ──本地 Agent Mail──▶ 本机 Hub (8765)
本机 agent CLI ──@Human handle──▶ 本机 Cockpit 回环代理 ──capability──▶ 远程 Team Hub
人 ──浏览器──▶ Cockpit 团队页 ──白名单代理──▶ Team Hub /hub/api/*
Human 登录态 ──▶ human_auth issuer (8766)
```

- 本地 agent 永远保留本机 Hub；**Team 通信单独走远程 Team Hub**（设置页配置）。
- TeamProject 是远程逻辑群组，**不绑定本机真实目录**；远程消息只读展示，绝不触发本地 shell/pane/worktree/任务。

### 5.2 角色

| 角色 | 来源 | 权限 |
|---|---|---|
| 全局 admin | JWT role 含 admin | 系统账号管理、邀请码、创建群组 |
| 群组 admin | membership role=admin | 本群成员审批/角色/绑定 |
| 普通成员 | active membership | 读目录、收发消息、绑定自己的 Session |

### 5.3 成员上手（当前主流程）

1. **注册账号**：团队页注册（需邀请码）→ 账号先 pending，管理员 activate 后登录。
2. **加入群组**：项目列表选群组 → 申请加入（handle 自动建议）→ 群组 admin 批准。
3. **绑定本机 Session**：项目页选一个运行中的本机 Session 点「绑定」——Cockpit 自动识别该 Session 的唯一 lead 角色及其本机 registry 身份；Hub 自动创建受管 Session Lead、设为你的默认 Agent、签发 reply capability。一步到位。

> 升级提示：早期版本创建的绑定没有 reply capability——在团队页重新选择绑定同一 Session 一次即可自动补发。

没有有效 Session 绑定时，团队页/终端 @协作不会发送（会明确提示去绑定）。

### 5.4 收发信息

- **人对团队**：团队页发群聊；终端 @协作可选择 @团队 或定向 @成员。
- **agent 回复人**：`mail-send --to @<Human handle> ...`——只允许"当前 Session 已绑定且 active 的 lead"身份发出；普通 developer/reviewer 不能绕过负责人直接对外。目标 Human 无可用路由时进入其**人工收件箱**。
- **人读信**：团队页「人工收件箱」（未读徽标），标为已读。
- **群聊显示**：来自受管 lead 的消息显示为「人类显示名 · via lead_label」。

### 5.5 管理员操作

- 生成一次性邀请码（24h 有效）；账号批准（pending→active）
- 项目内成员审批/角色/移除
- Session 绑定由成员本人在项目页操作，管理员无需代劳

---

## 6. 移动端（H5）

- 窄屏自动单栏；主操作在底部固定条
- 终端默认进流视图；attach 后自动 zoom 聚焦单 pane
- 工具栏可收起；安全区适配刘海屏

---

## 7. 排障 FAQ

| 症状 | 原因与处理 |
|---|---|
| `am-register` 报"缺少 token" | 未配本机 `~/.agent-mail/client.env` |
| 发信被拒"agent retired" | 闲置自动退休；重新运行 `am-register`/`am-init-project` 会自动恢复激活 |
| mail 工具返回 400 | 若日志明确是 `/mcp/` 路径：旧工具硬编码旧端点，升级 agent-mail-tools |
| 登录团队页被拒 | 账号 pending，需管理员 activate |
| 移除成员后 handle 不能再用 | 已知行为：removed 成员 handle 保留占用 |
| 发信 "Invalid recipient" | agent 邮箱只收 Agent Mail 花名；回复人类用 `@<Human handle>`；类型别名唯一时自动解析 |
| 团队消息发不出 | 先检查团队页是否已绑定本机 Session；未绑定不会发送 |
| **本地** Agent Mail 不通 | 查 client.env 的本机 Hub 地址/token、本机 Hub 进程 |
| **团队**消息不通 | 查设置页的 Team Hub/issuer 配置与网络，与本地 Hub 分开排查 |
| 终端右侧黑条/错位、文字太暗 | 均已修复（自适应尺寸 + 暗色对比度提升），普通刷新即可 |
| 并发同名注册偶发失败 | 既有基线竞态，重试即可 |

---

## 8. 附录

### 8.1 关键路径

| 内容 | 路径 |
|---|---|
| Cockpit 配置 | 部署目录 `.env` |
| 本地 Hub 连接 | `~/.agent-mail/client.env`（hub/token，绝不外泄） |
| agent 身份 | `~/.agent-mail/registry/<项目slug>/<类型>--<实例>.json` |
| 用户设置 | `~/dashboard-data/settings.json` |
| 文件白名单 | `~/.config/agent-cockpit/file-roots.json` |

### 8.2 端口

| 端口 | 服务 |
|---|---|
| 8790 | Agent Cockpit Web（本机） |
| 8765 | 本机 Agent Mail Hub；团队服务器同端口为 Team Hub |
| 8766 | human_auth（团队 JWT issuer） |

### 8.3 安全要点

- `COCKPIT_TOKEN`、Hub token、registration_token、reply capability token 都是凭据，不要贴进消息/日志/截图
- 敏感目录（~/.ssh、~/.agent-mail 等）不可加入文件白名单（系统强制）
- 远程 Hub 消息只读展示，永远不会自动执行本地动作

### 8.4 相关文档

- 团队设计背景：`docs/team-collaboration-design.md`（部分已演进，操作以本手册与当前实现为准）
