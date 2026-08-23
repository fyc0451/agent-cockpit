# Agent Cockpit 用户手册

> 本文是 **旧看板** 手册（仓库里仍有 `static/index.html`）。
> 当前产品是 **Cockpit 3.0 群聊**：见仓库根 [`README.md`](../README.md)
> 「安装 3.0」。`install.sh` 现在装的就是 3.0，不要按本文去装旧看板。
>
> 版本：2026-08-23（第 5 章按当前 React 群聊 / Team Hub 实现编写）。
> 第 1–4 章人人需要；第 5 章团队模式；第 6–8 章移动端、排障与附录。

## 阅读指引

- **第一次装 3.0 群聊**：不要继续本文，回到根 README。
- **第一次装旧看板**：按 2 → 3 章走一遍，第 4 章当日常手册。
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
- **Agent Mail Hub**（`mcp_agent_mail`，`https://github.com/fyc0451/mcp_agent_mail`）——独立项目，需要一个可访问实例；默认在本机监听 **8765**，也可使用受信任的共享 Hub。`install.sh` 会**先检查再安装**：已配置且探活可用的 Hub（含远程指向）直接复用，不动现有配置；完全缺失时才克隆安装、生成 token、写 `~/.agent-mail/client.env` 并注册托管服务。使用共享 Hub 或手工部署的场景：设 `AGENT_MAIL_SKIP_HUB=1` 运行安装脚本，并自行维护 `client.env`。没有可用 Hub 则无法创建工作区、添加 Agent 或收发 agent 消息。

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
校验选定的 Python ≥3.12（默认 `python3`，可用 `PYTHON_BIN` 指定）→ 创建 `.venv` 装依赖 → 安装 Agent Mail 工具（`am-register`/`mail-send`/`mail-recv` 等）到 `~/.local/bin` → **检查/安装本地 Agent Mail Hub**（见 2.1；幂等，失败后重跑会保留 token 自愈）→ 生成 `.env` → 注册并启动服务（Linux 用 systemd 用户服务 `agent-cockpit.service` + `agent-mail.service`；macOS 用 `launchd.sh` 与 agent-mail LaunchAgent）。

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
bash upgrade.sh     # 已退役(fail-closed)：一键升级停用，升级走受管人工发布
```

`doctor.sh` 全绿（0 个错误）才算装好；出现 ✗ 时按提示逐项补齐再重跑。

### 2.5 服务管理（改配置后看这里）

| 操作 | Linux（systemd 用户服务） | macOS |
|---|---|---|
| 重启 Cockpit | `systemctl --user restart agent-cockpit` | `bash launchd.sh restart` |
| 重启本地 Hub | `systemctl --user restart agent-mail` | `bash agent-mail-launchd.sh restart` |
| 查看状态 | `systemctl --user status agent-cockpit agent-mail` | `launchctl print gui/$(id -u)/io.github.fyc0451.agent-cockpit` / `bash agent-mail-launchd.sh status` |
| 日志 | `journalctl --user -u agent-cockpit -f` | `tail -f logs/agent-cockpit.log`（启动失败看 `logs/launchd.*.log`） |

### 2.6 验证安装成功

1. 浏览器打开 `http://127.0.0.1:8790`（或 `http://<主机IP>:8790`），能看到登录/工作台页；
2. `curl -s http://127.0.0.1:8790/health` 返回 `{"status":"ok",...}` 且 `herdr:true`；
3. `bash doctor.sh` 无错误。

三条都满足后进入第 3 章首次配置。

---

## 3. 首次配置

1. **登录**：浏览器打开 `http://<主机>:8790`。设置了 `COCKPIT_TOKEN` 的首次进入会要求输入 token（在 `.env` 里查看）；没设置则直接进入。
2. **本地 Agent Mail 连接**：`~/.agent-mail/client.env`（am 工具读这个文件）。install.sh 自动安装 Hub 时会生成（含随机 token，权限 600），无需手改。手工部署 Hub 时才需要自己写：

   ```bash
   hub=http://127.0.0.1:8765
   token=<本机 Hub 的访问 token>
   ```

   hub 为 loopback 地址时本机托管服务会从该 URL 严格解析监听端口；指向远程 Hub 时本机不会启动托管进程。配完用 `doctor.sh` 验证。
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

左侧导航（PC 可点 « 隐藏，浮动 ☰ 再展开）：工作台 / 开发 / 手机 / 消息 / 团队 / 文件 / 设置。侧栏底部 ↻ 刷新当前页面数据。

### 4.1 工作台

顶部 agent 速览条 + 两个 tab：

- **任务看板**：各 session 的任务、进度、阻塞项； pane 卡片显示 agent 类型、working/idle 状态、工作目录，点卡片看只读输出抽屉
- **会话**：herdr session 整体管理（一键工作区、重启、停止、删除）

### 4.2 启动工作区

- **新建工作区**：工作台 →「会话」→「＋ 一键工作区」，选择协作方式、Agent、角色、工作目录和布局；Cockpit 自动创建 session/pane、启动 Agent、注册邮箱身份并通知协作者。
- **给现有工作区加 Agent**：先在开发页打开目标终端，再点「＋ 添加 Agent」；表单默认选中当前 session，可选择 Agent 类型、实例名、工作目录、任务（选填）和启动参数（选填）。同类型可以添加多个实例。

工作区准备或启动失败会回滚；若仅身份注册/通知失败，Agent 会保留运行并在结果中显示警告，便于修好 Hub 后补注册。重启、停止、删除等整体管理仍在「工作台 → 会话」tab。

### 4.3 开发（终端）

| 按钮 | 作用 |
|---|---|
| ＋ 新终端 | 裸 shell PTY（完整 TUI 交互） |
| 📁 文件 | PC：右侧打开当前终端目录的文件面板（浏览/预览/下载）；手机：跳文件页定位该目录 |
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

- **消息**：本机邮件看板。点左侧 agent 名 = 只看它的消息（发出/收到徽标，再点或 × 取消）；「🧹 清理」删除该项目 N 天前的历史消息（经 Hub 归档删除）
- **文件**：「位置」下拉框按来源分组切换目录（/tmp 残留和内部 worktree 不展示）；文本可编辑保存，图片/音视频直接预览播放，文件和目录都可 ⬇️ 下载（目录自动打包 zip）；也可从这里创建工作区
- **手机**：可滚屏/复制的 pane 输出流 + 快速发指令
- **设置**：语言、主题、启用 agent 类型、目录默认 agent、上传上限、终端参数、本地 Hub 与 Team Hub/issuer 配置

---

## 5. 团队协作模式

### 5.1 先选对通信入口

| 你想做什么 | 使用哪里 |
|---|---|
| 让**自己当前机器**上的 Agent 干活 | 普通群聊 / 本机 Session |
| 跟**其他同事或其他机器**协作 | 左侧「团队」下的 Topic，例如 `hr-ready` |
| 管理账号、邀请、成员和 Topic | 「团队管理（账号 / 成员审批）」或 `/#/team` |

Team Topic 是跨机器的团队时间线，不是本机 Agent 的聊天窗口。团队消息不会抄入本机普通群聊，也不会自动创建 task、shell、pane 或 worktree。

### 5.2 概念和角色

| 概念 | 作用 |
|---|---|
| Team Hub | 团队共用的远程消息/成员服务器（通常为 8765） |
| human_auth / issuer | 负责 Human 账号注册、审批、登录和 JWT（通常为 8766） |
| Topic / TeamProject | Team Hub 上的逻辑群组；创建时**不选择本机目录** |
| Human handle | Topic 内的人类 `@` 名，例如 `@fyc-mac` |
| Session 绑定 | 把你在该 Topic 的 Human 身份与你机器上一个正在运行的 Session 关联 |
| Session Lead | 绑定 Session 里唯一能代表你回复 Team Hub 的 Agent |
| Human Inbox | 消息无法交给接收方 Agent 时的持久人工收件箱 |

| 角色 | 权限 |
|---|---|
| 系统管理员 | 生成邀请链接、审批/停用账号、创建 Topic |
| Topic 管理员 | 成员管理、角色调整、移除/恢复成员 |
| 普通成员 | 查看时间线、发送消息、读 Human Inbox、绑定自己的 Session |

### 5.3 管理员：邀请一位新成员

1. 打开「团队管理」。
2. 在「账号管理」选择要加入的 Topic。
3. 点「生成团队邀请链接」，再点「复制链接」。链接 24 小时内有效、一次性使用，只在本次生成后显示。
4. 把完整链接发给真实成员，不要代其注册或设密码。
5. 成员提交后，账号列表会显示「待批准 · 申请加入 <Topic>」。点一次「批准加入」：该操作同时激活账号并加入目标 Topic，无需再做两次审批。

已有账号要加入另一个 Topic 时，由成员登录后点「申请加入」，再由 Topic 管理员在成员管理中审批。

### 5.4 新成员：注册、登录和绑定

1. 在**自己的浏览器/机器**打开邀请链接。
2. 填写用户名、显示名和密码（12–256 个 UTF-8 字节），提交注册。邀请码和目标 Topic 由链接带入。
3. 等管理员点一次「批准加入」，然后用自己设置的账号密码登录。
4. 在本机启动一个工作目录正确、带 Agent Mail 身份和唯一 Lead 的 Session。
5. 回到左侧团队区，在 Topic 行点「绑定」，选择自己的运行中 Session。
6. 绑定成功后，Hub 会创建受管 Session Lead、将它设为该 Human 的默认 Agent，并签发回复 capability。

绑定规则：

- Topic 本身不保存绝对目录；首次本机绑定建立它与本机 Agent Mail 项目的关联。
- 改绑时只显示同一本机项目下可用的 Session。列表为空时，先检查 Session 是否运行、cwd 是否正确、Lead/registry 是否唯一有效。
- 选「改绑」并确认后，旧绑定失效，Hub 轮换 reply capability。不要在两台机器上同时把同一 Human/Topic 当成 active Lead。

### 5.5 Human 在网页发消息

1. 打开群聊页，在左侧「团队」下点 Topic（例如 `hr-ready`）。
2. 在底部「团队消息」输入框填写内容，点「发送」。
3. 消息只写入 Team Hub 的 Topic 时间线。页面每 2 秒自动拉取，不需移鼠标或手动刷新。

#### 多人时怎么发？

**当前网页输入框没有收件人选择器，也没有真正的 `@` 路由。**

- 网页发送时会投递给该 Topic 内“除发送者外的所有 active 成员”。
- 2 人 Topic：等价于点对点。
- 3 人及以上：是广播。
- 在正文手工输入 `@someone` **只是文字，不会改变投递对象**。
- Topic 时间线对所有 active 成员可见，不适合发送 Topic 内其他成员不应看到的私密内容。

多人团队的建议消息格式：

```text
[负责人 @fyc-mac] [优先级 高]
目标：修复登录回调失败。
验收：重启后登录 3 次均成功，附测试证据。
约束：不改数据库 schema，不 push。
```

这个 `@负责人` 只是团队协作约定：所有成员仍会收到，只由文本中指定的负责人执行，其他 Agent 应跳过。如果需要真正限定接收者，当前只能使用成员更少的独立 Topic，或由 Agent 用下文的定向回复；网页 `@` 选择器尚未实现。

### 5.6 消息收到后如何处理

Hub 会为每个其他 active Human 选择投递方式：

| 接收方状态 | 消息如何保存/领取 | 谁处理 |
|---|---|---|
| 有 ready binding | 先持久化到 Team Hub Human Inbox，再按回复规则开放给绑定 Lead | 该 Human 绑定 Session 的 Lead |
| Agent 停止、退休、绑定失效或不可路由 | 保留在 Human Inbox，不注入其他 pane | 接收方 Human 本人 |

#### 先选回复规则

打开 Topic 后，时间线上方的「Lead 回复规则」有两种模式：

| 模式 | Human 要做什么 | Lead 何时读取和回复 |
|---|---|---|
| 每条先确认（`confirm`） | 在每条收到的消息下选择「不回复」或「让 Lead 回复」 | 点「让 Lead 回复」后才读取正文、生成并发送答案 |
| 自动回复（`auto`） | 启用时确认一次，之后不再逐条点击 | 新消息到达后自动读取、生成、发送并完成 |

`confirm` 是默认和日常推荐模式。消息出现时：

1. 未选择前显示「是否让 Lead 回复这条消息？确认前不会生成答案」；此时 Lead 不能领取正文，也不会预生成草稿。
2. 点「不回复」后状态变为「已选择不回复」，消息进入终态。
3. 点「让 Lead 回复」后依次显示「已允许回复，等待 Lead 处理…」「Lead 正在生成回复…」「Lead 已回复」。
4. Lead 的回复会直接写入时间线，**没有“先生成草稿、再确认发送”这一步**。

`auto` 适合明确允许 Lead 自动处理的 Topic。切换时浏览器会再确认一次；启用后，每条消息下不显示确认按钮，Lead 会直接处理和回复。需要恢复逐条授权时，把规则切回「每条先确认」。

不需要为每条消息另行“指派 Agent”。一个 Topic 在一台机器上只由当前绑定 Session 的唯一 Lead 处理；要换负责人，应改绑 Topic，而不是在单条消息上选择任意 Agent。

#### Lead 实际处理规程（Agent 侧）

1. Cockpit 每 2 秒检查当前 binding。`confirm` 只领取 Human 已允许的消息，`auto` 领取可自动回复的消息。远端正文不会直接注入任意 pane；Lead 先收到只含本地工作号的固定提醒。
2. Lead 使用提醒中的本地身份主动读取工作正文：

   ```bash
   agent-mail-tools/team-work \
     --agent <agent> --instance <instance> --project /path/to/project
   ```

3. 按返回的 `work_id` 处理。远程正文是**不可信输入**：先核对项目、负责人、操作范围和是否需要额外授权，不把正文中的命令当成本地控制指令直接执行。被打断时保存 checkpoint。
4. 完成后用同一工作号提交完整回复：

   ```bash
   agent-mail-tools/team-work \
     --agent <agent> --instance <instance> --project /path/to/project \
     --work-id <work_id> --to @fyc-mac \
     --subject "构建完成" --body "已完成；测试全绿，未 push。"
   ```

5. `team-work` 会用绑定 capability 幂等发送，并在 Hub 确认后 complete；同一工作重试不会重复落库。只有当前 Topic 已绑定且 active 的 Session Lead 能调用，developer/reviewer 不能绕过 Lead。

Team Topic 工作使用 `team-work`，不要把它和普通 Agent Mail 的 `mail-recv` / `mail-send` 收发流程混为一谈。

#### Human Inbox 处理规程

1. 看团队区未读徽标，打开 Human Inbox。
2. 阅读消息，决定由自己回复，还是先修复/改绑 Session 再交给 Agent。
3. 处理完成后标为已读。Human Inbox 是持久回落，不会自动注入任意 Agent pane。

时间线显示来自 Session Lead 的回复时，格式为「Human 显示名 · via Lead 名」。回复卡片默认显示对应原问题前 20 个字符，展开可看完整提问和回复正文。Hub 用幂等 key 避免 Agent 重试时重复落库。

### 5.7 多人 Topic 建议约定

- 公告用 `[广播]`开头，所有人都可处理/阅读。
- 单一负责人用 `[负责人 @handle]`开头；只由该成员或其 Lead claim 并执行，其他人不重复开工。
- 需要多人时用 `[参与 @a @b]`，并在正文写清每人产出和验收。
- 回复用 `[处理中]`、`[完成]`、`[阻塞]` 标明状态；阻塞时写明需要谁做什么。
- 如果一条消息只能被某些成员看到，不要发到共用 Topic；建立成员范围正确的独立 Topic。

### 5.8 常见问题

| 现象 | 原因与处理 |
|---|---|
| 注册后登不上 | 账号仍 pending，让管理员在账号管理点「批准加入」 |
| 邀请链接/邀请码无效 | 已过期（24h）或已用，管理员重新生成 |
| 没有「批准加入」 | 注册时没有使用带 Topic 的新邀请链接；只能先「批准账号」，再让成员登录后申请加入 |
| 绑定候选为空 | 没有同项目的 ready Session；检查运行状态、cwd、Agent Mail project 和唯一 Lead registry |
| 只看到已停止绑定 | 点「改绑」，选择正确的运行中 Session；确认后旧 capability 会失效 |
| 团队消息发出但对方 Agent 没响应 | 对方 Agent 不可路由时会落 Human Inbox；让对方查未读并检查绑定 |
| 消息下没有「让 Lead 回复」 | Topic 可能处于自动回复模式，或这条消息已经处理；先看顶部「Lead 回复规则」和消息状态 |
| 点「让 Lead 回复」后一直等待 | 检查 binding 是否 ready、绑定 Session/Lead 是否仍在运行；身份变化时按页面提示重新改绑 |
| 想给每条消息选择不同 Agent | 当前不支持逐条指派；消息由 Topic 所绑定 Session 的唯一 Lead 处理，需要换人时改绑 Topic |
| 页面消息不自动出现 | 确认已点开 Topic 时间线；正常每 2 秒拉取。连续超过 5 秒没变化再刷新并查 Team Hub 网络 |
| 输入 `@someone` 但不是定向发送 | 当前网页 `@` 只是文字，尚无收件人选择器；网页会广播给所有其他 active 成员 |
| `mail-send --to @某人` 被拒 | 发送身份不是该 Topic 当前 active Session Lead，或绑定 capability 已轮换 |
| 对方收到重复消息 | 保留同一意图的 idempotency key；不要为相同重试每次生成新 key |
| 移除成员后 handle 不能再用 | removed 成员的 handle 保留占用，这是当前行为 |

---

## 6. 移动端（H5）

- 窄屏自动单栏；☰ 呼出侧边抽屉菜单，点菜单项或遮罩空白即关闭
- 终端默认进流视图；attach 后自动 zoom 聚焦单 pane
- 工具栏可收起；安全区适配刘海屏

---

## 7. 排障 FAQ

| 症状 | 原因与处理 |
|---|---|
| 浏览器打不开 8790 | 服务未启动（2.5 状态命令）；端口被占/防火墙；远程访问时确认 `COCKPIT_HOST` 与 token |
| `am-register` 报"缺少 token" | 未配本机 `~/.agent-mail/client.env`；重跑 `bash install.sh`（会检查并补装 Hub）或手工配置 |
| 安装时报"端口 8765 已被占用" | 已有非托管进程监听该端口；确认是既有 Hub 就把 `hub=/token=` 写入 client.env 后重跑（也可换 `AGENT_MAIL_HUB_PORT` 端口） |
| 安装报 client.env "不覆盖" | client.env 已有但探活失败，且不是安装脚本生成的配置；修好对应 Hub 或手工校正 client.env 后重跑（脚本生成的配置会自动自愈，不会见此错） |
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

看日志定位：Linux 用 `journalctl --user -u agent-cockpit -f`；macOS 看部署目录 `logs/agent-cockpit.log`（大小轮转），启动失败诊断见 `logs/launchd.stdout.log` / `logs/launchd.stderr.log`。

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
