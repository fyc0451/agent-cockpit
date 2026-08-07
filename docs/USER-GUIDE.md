# Agent Cockpit 用户手册

> 版本：2026-08-07（适配 Cockpit origin/main ≥ 6daf313；Team Hub ≥ f74fb58）。
> 第 1–4 章人人需要；第 5 章团队模式；第 6–8 章移动端、排障与附录。

## 阅读指引

- **第一次装**：按 2 → 3 章走一遍，第 4 章当日常手册。
- **只加入已有团队**：看 5.1 概念表 + 5.4 成员上手，管理员操作不用管。
- **搭团队的管理员**：5.1 → 5.3 → 5.5 按顺序读。
- **出错先查第 7 章 FAQ**，按症状对号入座。

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
| 本地 Agent Mail 通道 | agent 间消息，**独立部署、必需** | 默认连接本机 Hub 8765，也可连接受信任的共享 Hub |
| Team Hub（远程） | 团队消息/成员/群组 | 团队服务器 8765 |
| human_auth（远程） | 团队 Human 登录/JWT 签发 | 团队服务器 8766 |

**关键边界**：

- 每个 Cockpit 只管理**本机**的 Herdr/session/终端；跨机只有 Team 消息通信，**不远控其他机器**。
- **本地 Agent Mail 与远程 Team Hub 是两条独立链路**：`client.env` 只控制本地 Agent Mail；Team Hub / Human issuer 在设置页分别配置。这里的“本地”指本机 agent 使用的消息通道，Hub 进程可在本机，也可部署为受信任的共享服务；agent 回复人类则经本机 Cockpit 的回环 reply 代理 + Session 绑定 capability 到远程 Team Hub。
- Agent Mail Hub 是**前置依赖**：创建工作区、添加 Agent 都要求 Hub 在线且可写；Hub 挂掉时禁止新增，已有本地消息在数据库仍可读时可继续只读查看。

**身份模型**（先理解这几个名词，后文不再解释）：

| 名词 | 是什么 | 例子 |
|---|---|---|
| herdr session | 一组终端的容器（= 一个工作区） | `hr-ready` |
| pane | session 内的一个终端窗口，跑一个 agent CLI | codex 的 pane |
| 项目（project） | 一个本地目录（通常是 git 仓库根），agent 邮箱按项目隔离 | `/home/fyc/hr-ready` |
| registry 身份文件 | `~/.agent-mail/registry/<项目slug>/<类型>--<实例>.json`，**文件名只是本地 selector** | `kimi--main.json` |
| **Agent Mail 花名** | 注册时 Hub 返回、存在身份文件 `name` 字段的收件人名 | `RedHawk`、`codex-main` |
| Human / Human handle | 团队里**人类**的账号与 `@` 名 | `付彦超` / `@fyc-mac` |

> 花名不保证是 `codex-main`，可能是 `RedHawk` 这类随机名；发信以身份文件里的 `name` 为准。Human handle 只用于 `mail-send --to @<handle>` 回复人类，**不是 agent 收件人**，两者不要混用。

---

## 2. 安装（从零开始）

### 2.1 环境要求

- Linux（含 WSL）或 macOS；Python ≥ 3.12；Git；curl
- 各 agent CLI（codex / kimi / claude / qoder / opencode / grok 等）按需在 PATH；Herdr 可用
- **Agent Mail Hub**（`mcp_agent_mail`，`https://github.com/fyc0451/mcp_agent_mail`）——独立项目，需先按它自己的文档部署一个可访问实例；默认是在本机监听 **8765**，也可使用受信任的共享 Hub。Cockpit 的安装脚本**不包含** Hub；没有可用 Hub 则无法创建工作区、添加 Agent 或收发 agent 消息。

### 2.2 安装 Cockpit

二选一：

```bash
# 方式 A：一键脚本
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash

# 方式 B：clone 安装
git clone https://github.com/fyc0451/agent-cockpit.git ~/agent-cockpit
cd ~/agent-cockpit
bash install.sh
```

install.sh 会校验现有 checkout（只有 curl 直装方式才自动 clone），然后依次：
Python ≥3.12 检查 → 创建 `.venv` 装依赖 → 安装 Agent Mail 工具（`am-register`/`mail-send`/`mail-recv` 等）到 `~/.local/bin` → 生成 `.env` → 注册并启动服务（Linux 用 systemd 用户服务 `agent-cockpit.service`；macOS 用 `launchd.sh`）。

### 2.3 配置文件 `.env`

部署目录中的 `.env`（默认 `~/agent-cockpit/.env`，安装时自动生成）：

```bash
COCKPIT_HOST=127.0.0.1      # 默认仅本机访问（推荐）
COCKPIT_PORT=8790
```

需要**手机/局域网其他设备**访问时，改成：

```bash
COCKPIT_HOST=0.0.0.0
COCKPIT_TOKEN=<随机串>      # 监听非本机地址时必须同时设置 token
```

生成随机串：`python -c 'import secrets; print(secrets.token_urlsafe(32))'`。**改完必须重启服务**（见 2.5）。

> 安全提示：8790 监听 `0.0.0.0` 时，对外暴露必须配合 token，公网环境建议再套 HTTPS/Tailscale。

### 2.4 自检与升级

```bash
bash doctor.sh      # herdr / 各 agent CLI / Agent Mail 数据库与 client.env 就绪检查
bash upgrade.sh     # 一键升级：拉代码 + 装依赖 + 重启服务（Linux/macOS 通用）
```

`doctor.sh` 全绿（0 个错误）才算装好；出现 ✗ 时按提示逐项补齐再重跑。

### 2.5 服务管理（改配置后看这里）

| 操作 | Linux（systemd 用户服务） | macOS |
|---|---|---|
| 重启 | `systemctl --user restart agent-cockpit` | `bash launchd.sh restart` |
| 查看状态 | `systemctl --user status agent-cockpit` | `launchctl print gui/$(id -u)/io.github.fyc0451.agent-cockpit` |
| 日志 | `journalctl --user -u agent-cockpit -f` | `tail -f agent-cockpit.stdout.log agent-cockpit.stderr.log` |

### 2.6 验证安装成功

1. 浏览器打开 `http://127.0.0.1:8790`（或 `http://<主机IP>:8790`），能看到登录/看板页；
2. `curl -s http://127.0.0.1:8790/health` 返回 `{"status":"ok",...}` 且 `herdr:true`；
3. `bash doctor.sh` 无错误。

三条都满足后进入第 3 章首次配置。

---

## 3. 首次配置

1. **登录**：浏览器打开 `http://<主机>:8790`。设置了 `COCKPIT_TOKEN` 的首次进入会要求输入 token（在 `.env` 里查看）；没设置则直接进入。
2. **本地 Agent Mail 连接**：编辑 `~/.agent-mail/client.env`（am 工具读这个文件）：

   ```bash
   hub=http://127.0.0.1:8765
   token=<本机 Hub 的访问 token>
   ```

   token 由本机 Agent Mail Hub 的部署配置给出（见 `mcp_agent_mail` 部署文档）。配完用 `doctor.sh` 验证。
3. **团队通道（可选，用到第 5 章才需要）**：浏览器 设置 → Agent Mail 区域，填 **Team Hub API**（如 `http://<团队服务器>:8765`）与 **Human issuer**（如 `http://<团队服务器>:8766`），保存即生效——与本地 Hub 互不影响。
4. **文件目录白名单**：设置/文件页添加可访问根目录（整 Home 不可添加；建议加 `~/github` 这类顶层目录）。
5. **注册邮箱身份**（每个项目一次，终端里执行）：

```bash
cd /path/to/project
am-init-project          # 批量注册全部已知 agent 类型
# 或单个注册:
am-register --agent kimi --instance main --project /path/to/project
```

输出会列出每个 agent 注册到的**花名**（如 `kimi ✓ 注册 WindyBarn`）。之后随时可以查看自己的花名：

```bash
am-register --agent kimi --instance main --project /path/to/project --show
cat ~/.agent-mail/registry/<项目slug>/kimi--main.json   # name 字段即花名
```

> 重复运行是安全的：已有身份自动复用；身份被闲置退休（retired）时会持 registration token 自动恢复激活。

---

## 4. 个人模式日常使用

### 4.1 看板

各 herdr session 的 pane 卡片：agent 类型、working/idle 状态、工作目录。点卡片看只读输出抽屉。

### 4.2 启动工作区

终端页 →「＋ 添加 Agent」→ 选 session（可新建）/agent 类型/工作目录/任务/布局 → 自动建 pane、启动、注册邮箱身份、通知协作者。工作区准备或启动失败会回滚；若仅身份注册/通知失败，Agent 会保留运行并在结果中显示警告，便于修好 Hub 后补注册。同类型可开多实例。Sessions 页可整体管理 session（创建/重启/停止）。

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
# 回复人类成员(团队模式,见 5.5)
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
- Hub 最终接收的 agent 收件人是**花名**；命令行也可输入能唯一解析的 agent 类型别名。回复人类要用 `@<Human handle>`（团队模式）
- 协作约定：里程碑查未读；先 claim 后 complete/fail；停止/转向先存 checkpoint 再停手

**最小示例**（同一项目两个 agent 互发消息）：

```bash
# codex 给 kimi 发问
mail-send --agent codex --instance main --project /path/to/project \
  --to WindyBarn --subject "接口确认" --body "登录接口字段变了吗？"
# kimi 查未读并处理
mail-recv --agent kimi --instance main --project /path/to/project --unread
```

### 4.5 其他页面

- **任务**：跨项目 run 与待办（blocked/失败/待审）
- **消息**：本机邮件看板
- **文件**：白名单目录浏览/编辑/下载，也可从这里创建工作区
- **设置**：语言、启用 agent 类型、目录默认 agent、上传上限、终端参数、本地 Hub 与 Team Hub/issuer 配置

---

## 5. 团队协作模式

### 5.1 概念表（先读这个）

| 概念 | 一句话解释 |
|---|---|
| Team Hub | 团队共用的远程消息/成员服务器（`mcp_agent_mail` 团队功能，端口 8765） |
| human_auth / issuer | 给人类账号签发登录 JWT 的服务（端口 8766） |
| TeamProject（群组） | Team Hub 上的**远程逻辑群组**，不绑定任何机器的真实目录 |
| Human | 团队里的人类账号；`@` 名（handle）用于被 agent 回复 |
| 邀请码 | 管理员生成的一次性码（24h 有效），注册账号用 |
| Session 绑定 | 把"本机一个运行中的 herdr session"与你在群组里的身份挂钩 |
| Session lead | 绑定后 Hub 为这个 session 创建的受管代表身份；**只有它能代表你对外回复** |
| reply capability | 绑定时签发的凭据，让本机 lead 的回复能投递回 Team Hub |
| 人工收件箱 | 远程发给某个 Human 但暂无法路由到其终端的消息暂存处，团队页可读 |

**架构与边界**：

```
本机 agent CLI ──本地 Agent Mail──▶ 本机 Hub (8765)
本机 agent CLI ──@Human handle──▶ 本机 Cockpit 回环代理 ──capability──▶ 远程 Team Hub
人 ──浏览器──▶ Cockpit 团队页 ──白名单代理──▶ Team Hub /hub/api/*
Human 登录态 ──▶ human_auth issuer (8766)
```

- 本地 agent 永远保留本机 Hub；**Team 通信单独走远程 Team Hub**（设置页配置）。
- TeamProject 不绑定本机真实目录；远程消息不会创建 shell、pane、worktree 或任务。属于已绑定项目的 Human Inbox 消息会被标记为“不可信远程文本”，投递到现有 Session lead 的 pane 供其处理。

### 5.2 角色

| 角色 | 来源 | 权限 |
|---|---|---|
| 全局 admin | JWT role 含 admin | 系统账号管理、邀请码、创建群组 |
| 群组 admin | membership role=admin | 本群成员审批、角色与移除 |
| 普通成员 | active membership | 查看群组/成员、收发消息、绑定自己的 Session |

### 5.3 管理员上手（搭团队）

前提：团队服务器已按 `mcp_agent_mail` 团队部署文档拉起 Team Hub（8765）与 human_auth issuer（8766），管理员在 Cockpit 设置页填好这两个地址。

1. **创建群组**：团队页新增 TeamProject（逻辑群组，只起名字，不选本机目录）。
2. **生成邀请码**：团队管理 → 生成一次性邀请码（24h 有效），把码发给要加入的人。
3. **批准账号**：成员用邀请码注册后账号是 pending，团队页把其激活（pending → active）后对方才能登录。
4. **审批入群**：成员申请加入群组后，群组 admin 批准并可设角色（admin/普通成员）。
5. Session 绑定由成员本人在项目页操作，**管理员无需也不能代劳**。

### 5.4 成员上手（加入已有团队）

1. **注册账号**：团队页 → 注册（输邀请码）→ 账号先 pending，等管理员 activate 后再登录。
2. **登录**：用注册的账号登录 Cockpit 的团队入口（登录态来自 human_auth issuer）。
3. **加入群组**：项目列表选中群组 → 申请加入（handle 自动建议，可自定义）→ 等群组 admin 批准。
4. **启动一个本机 session**（要已有一个运行中的才能绑定）：按 4.2 建工作区或复用现有 session。
5. **绑定 Session**：团队页项目页选该运行中的 Session 点「绑定」——Cockpit 自动识别该 Session 的唯一 lead 角色及其本机 registry 身份；Hub 自动创建受管 Session Lead、设为你的默认 Agent、签发 reply capability。一步到位。
   - 没有运行中/可用 session 时无法绑定，团队页会提示。
   - 没有有效 Session 绑定时，团队页/终端 @协作**不会发送**（会明确提示去绑定）。

> 升级提示：早期版本创建的绑定没有 reply capability——在团队页重新选择绑定同一 Session 一次即可自动补发。

### 5.5 收发信息（端到端示例）

**人对团队**：

- 团队页直接发群聊消息；
- 终端 @协作 可选择 @团队 或定向 @成员（消息经本机 Cockpit 代理发到 Team Hub）。

**团队消息进入本机终端（人 → agent）**：

1. 人在团队页/@协作发出消息；
2. 已绑定的 Session lead 在本机终端收到该消息——以"不可信远程文本"提交给 lead；
3. Cockpit 同时生成**只含本机可信身份、项目和收件人的幂等回复模板**；lead 必须通过该模板发送完整非空正文，不能只在本机终端口头回答；
4. 自动生成的回复不会再次触发自动回执，避免双方 Agent 循环互答。

**agent 回复人类（agent → 人）**：

```bash
mail-send --agent kimi --instance main --project /path/to/project \
  --to @fyc-mac --subject "构建完成" --body "已按方案 A 完成，见附件说明。"
```

- 只允许"**当前 Session 已绑定且 active 的 lead**"身份发出；普通 developer/reviewer 身份不能绕过负责人直接对外。
- 目标 Human 无可用路由（对方 Cockpit 离线等）时，消息进入对方的**人工收件箱**。

**人读信**：团队页「人工收件箱」（未读徽标）→ 点开阅读 → 标为已读。

**群聊显示规则**：来自受管 lead 的消息显示为「人类显示名 · via lead_label」。

### 5.6 团队常见问题速查

| 现象 | 原因与处理 |
|---|---|
| 注册后登不上 | 账号仍 pending，等管理员 activate |
| 邀请码无效 | 过期（24h）或已用过，找管理员重新生成 |
| @协作/团队消息发不出 | 未绑定 Session；到团队页绑定一个运行中的本机 Session |
| 绑定了但 reply 失败 | 旧绑定缺 capability；重新选同一 Session 绑定一次补发 |
| `mail-send --to @某人` 被拒 | 发送身份不是该 Session 的 active lead（developer/reviewer 无对外权限） |
| 对方说没收到 | 消息可能进了对方的人工收件箱，让对方看团队页未读徽标 |
| 移除成员后 handle 不能再用 | 已知行为：removed 成员 handle 保留占用 |

---

## 6. 移动端（H5）

- 窄屏自动单栏；主操作在底部固定条
- 终端默认进流视图；attach 后自动 zoom 聚焦单 pane
- 工具栏可收起；安全区适配刘海屏

---

## 7. 排障 FAQ

| 症状 | 原因与处理 |
|---|---|
| 浏览器打不开 8790 | 服务未启动（2.5 状态命令）；端口被占/防火墙；远程访问时确认 `COCKPIT_HOST` 与 token |
| `am-register` 报"缺少 token" | 未配本机 `~/.agent-mail/client.env` |
| 创建工作区报缺 Agent Mail | `client.env` 指向的 Hub 不可达或不可写；使用默认本机部署时确认 8765 正在监听，`doctor.sh` 会给出具体原因 |
| 发信被拒"agent retired" | 闲置自动退休；重新运行 `am-register`/`am-init-project` 会自动恢复激活 |
| mail 工具返回 400 | 若日志明确是 `/mcp/` 路径：旧工具硬编码旧端点，升级 agent-mail-tools |
| 登录团队页面被拒 | 账号 pending，需管理员 activate |
| 发信 "Invalid recipient" | agent 邮箱只收 Agent Mail 花名；回复人类用 `@<Human handle>`；类型别名唯一时自动解析 |
| 团队消息发不出 | 先检查团队页是否已绑定本机 Session；未绑定不会发送 |
| **本地** Agent Mail 不通 | 查 client.env 的本机 Hub 地址/token、本机 Hub 进程 |
| **团队**消息不通 | 查设置页的 Team Hub/issuer 配置与网络，与本地 Hub 分开排查 |
| 终端右侧黑条/错位、文字太暗 | 均已修复（自适应尺寸 + 暗色对比度提升），普通刷新即可 |
| 并发同名注册偶发失败 | 既有基线竞态，重试即可 |

看日志定位：Linux 用 `journalctl --user -u agent-cockpit -f`；macOS 看部署目录中的 `agent-cockpit.stdout.log` 和 `agent-cockpit.stderr.log`。

---

## 8. 附录

### 8.1 关键路径

| 内容 | 路径 |
|---|---|
| Cockpit 配置 | 部署目录 `.env` |
| 本地 Hub 连接 | `~/.agent-mail/client.env`（hub/token，绝不外泄） |
| agent 身份 | `~/.agent-mail/registry/<项目slug>/<类型>--<实例>.json` |
| am/mail 工具 | `~/.local/bin/am-*`、`~/.local/bin/mail-*` |
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
- 远程消息仅作为带安全标记的不可信文本投递给已绑定 lead；不会直接执行正文中的 shell、部署、删除、权限或凭据操作

### 8.4 相关文档

- 团队设计背景：`docs/team-collaboration-design.md`（部分已演进，操作以本手册与当前实现为准）
