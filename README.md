# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> 跑在浏览器里的 CLI 编码 agent 驾驶舱:配合 [herdr](https://herdr.dev) 使用,
> 一眼看清每个 agent 的状态,随时接管终端、发指令、传截图、跨 agent 协作——
> 电脑和手机浏览器都能用。

[English](README.en.md) | [日本語](README.ja.md)

## 当前版本：本机群聊 + Team Topic

当前安装包同时包含两种互不混账的协作方式：

- **本机群聊（3.0）**：控制本机 herdr 里的 CLI agent，支持排队、打断、附件、
  终端接管和 Agent Mail 协作。
- **Team Topic（4.0）**：让不同机器上的真实成员通过共享 Team Hub 收发消息；
  每位成员把 Topic 绑定到自己的一个运行中 Session，由唯一 Lead 按 `auto` 或
  `confirm` 规则回复。

两者共用一个 Web 入口：`http://127.0.0.1:8790/#/chat`。`install.sh` 会编译
`web/dist`、安装本机 Agent Mail 工具并注册后台服务；旧看板不再作为安装结果。

## 快速安装

和 herdr 装在同一台机器上。需要：

| 依赖 | 用途 |
| --- | --- |
| [herdr](https://herdr.dev) | agent 所在的终端会话；缺失时由安装器自动安装 |
| Git、Python 3.12+、Node.js 20+（含 npm） | 拉代码、跑服务、编译 `web/dist` |
| [Agent Mail](https://github.com/fyc0451/mcp_agent_mail) Hub（默认 `:8765`） | 身份和本机协作；安装器会检查、复用或安装 |
| 至少一个已登录的 Agent CLI | Codex / Claude / Kimi / OpenCode / Grok / Qoder CLI CN |

仓库可以 clone 到任意路径。不必建 `$HOME/github`。发现根默认是仓库的上一级
目录；clone 就在 Home 下一层时，用仓库自己。要扫别的代码目录时再设
`COCKPIT_PROJECT_ROOT`（必须是已存在的真实目录，不能是 Home 本身）。

### 方式一：clone 后安装（推荐）

私有仓库协作者要先获得仓库权限并配置 GitHub SSH key。也可以运行
`gh auth login` 后用 `gh repo clone fyc0451/agent-cockpit`。不要下载单个
`install.sh`：安装器还依赖仓库里的 service、前端和辅助脚本。

```bash
git clone git@github.com:fyc0451/agent-cockpit.git
cd agent-cockpit
./install.sh
./doctor.sh
```

仓库公开时也可以使用 HTTPS：

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
./install.sh
./doctor.sh
```

安装器会在当前 checkout 建 venv，自动安装/复用 Herdr、Agent Mail 命令与本机
Hub，并为本机检测到的 Agent CLI 安装 Herdr 集成，然后编译 `web/dist`，注册
`agent-cockpit.service`（macOS 为 LaunchAgent）。启动后打开
`http://127.0.0.1:8790/#/chat`。重复运行安装器是安全的，可用于修复缺失依赖。

Codex、Claude、Kimi、OpenCode、Grok、Qoder CLI CN 等 **Agent CLI 本身不会由
Cockpit 安装**，请按需自行安装并完成登录。Cockpit 的「添加成员 / 添加 Agent」
只显示本机实际存在且可执行的 CLI；新装 CLI 后重跑 `./upgrade.sh` 即可补齐集成。

已经有可用的 Agent Mail Hub 时会复用，不会覆盖手工/远程的
`~/.agent-mail/client.env`。跳过本机 Hub 时设 `AGENT_MAIL_SKIP_HUB=1`。
启动失败先跑 `./doctor.sh`。8790 被占用时先停掉占用该口的进程，不要改端口。

不要直接 `.venv/bin/python server.py`：没有 `COCKPIT_NEXT_PROFILE=dev` 时
首页不是 3.0。服务单元已经走 `scripts/dev_server.py`。

### 方式二：只在前台运行

不注册 systemd/launchd 时，同样要编译前端并用当前启动器：

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./install-herdr.sh .
./install-agent-mail-tools.sh .
./install-agent-mail-hub.sh
npm ci --prefix web
npm run --prefix web build
.venv/bin/python scripts/dev_server.py
# → http://127.0.0.1:8790/#/chat
```

### 局域网（可选）

```bash
install -d -m 700 "$HOME/.config/agent-cockpit"
(umask 077; set -o noclobber; openssl rand -hex 32 > \
  "$HOME/.config/agent-cockpit/cockpit.token")
COCKPIT_HOST=0.0.0.0 .venv/bin/python scripts/dev_server.py
```

浏览器打开 `http://<本机局域网IP>:8790`，登录时粘贴
`~/.config/agent-cockpit/cockpit.token` 的内容。不要把令牌写进 `.env`、聊天或日志。

> **安全警告:** 远程访问请用 HTTPS 或 Tailscale Serve。裸 HTTP 会让登录
> cookie 暴露给同网段的任何人。不要把 Agent Cockpit 直接暴露到公网。

### 入口说明

| 入口 | 实际结果 |
| --- | --- |
| `scripts/next_dev.py` / `:18790` | 已冻结的 Next 2.0 隔离预览，不是当前 3.0 |
| `./upgrade.sh` | 源码版一键升级：跟踪当前分支上游，失败自动回滚 |
| 打开 GitHub Latest 的 native V2 | 会把源码 8790 换成打包单元 |

源码 8790 装好之后，在安装目录运行 `./upgrade.sh` 即可拉取当前分支上游、重装
依赖、重建 `web/dist`、重启源码单元并检查 `/health/live`。旧 V1 Web 升级 API
仍保持退役，不要打开 `COCKPIT_UPGRADE_V2_ENABLED`。

## 功能一览

- **群聊瀑布流** — herdr 里的 CLI agent 作为成员出现；结论进气泡，过程可展开。
- **排队发送（默认）** — Enter 排队，等对方空闲再投；要停手头工作才点「打断」。
- **Harvest** — 只在 pane `idle` / `done` 时收结论；busy 时不改上一条气泡。
- **文件与附件** — 群聊里看仓库、复制路径；附件默认折叠。
- **设置** — 外观和环境自检；源码一键升级从安装目录执行 `./upgrade.sh`。
- **移动端** — Hash 路由，手机浏览器可打开同一群聊。
- **Team Topic** — 邀请注册、成员审批、跨机器时间线、Session 绑定，以及
  `auto` / `confirm` 两种受限 Lead 回复模式。

## 工作原理

```
浏览器(电脑 / 手机)
    │  局域网 / VPN(:8790)
    ▼
Agent Cockpit(FastAPI,与 herdr 同机)
    ├── 可选只读本机 Agent Mail SQLite(WAL 模式)
    ├── 经本机或远程 Agent Mail hub MCP 写入
    ├── 读 herdr socket(所有 session)(pane 状态 / 输出)
    ├── 可选连接团队共享 Team Hub + Human issuer
    └── 经 SSE 向浏览器推送状态与 diff
```

部署在**和 herdr 同一台机器**上;电脑和手机只是浏览器客户端。Agent Mail Hub
可以部署在同机，也可以使用团队共享的远程服务；远程模式没有本机 SQLite 时，
本地消息列表会降级，但注册身份、创建工作区和添加 Agent 仍可正常使用。
Agent Mail 是新建工作区和添加 Agent 的前置条件；hub 暂时挂掉时禁止新增，已有消息只读，
群聊里已有气泡和 herdr pane 仍可查看。

## 首次使用：本机 Agent

1. 确认 herdr 已在跑，并且至少有一个已登录的 agent pane。
2. 浏览器打开 `http://127.0.0.1:8790/#/chat`。
3. 左侧选工作区 / herdr session；右侧成员栏能看到花名。
4. 输入框默认是 **排队**。`@` 成员后回车，对方空闲才会开始做。
   只有要停手头工作时才点 **打断**。
5. 回复先进瀑布流。长过程折在「展开过程」里，结论在气泡里。
6. 设置页可改外观、跑环境自检、配置 Team Hub。

Hash 路由：群聊 `/#/chat`，设置 `/#/settings`。旧路径会落到群聊。

旧看板只作为仓库里的 `static/index.html` 残留，当前安装入口不再启动它。

## 首次使用：加入现有团队

普通成员不需要自己部署团队服务器。先向管理员索取三样东西：

1. Team Hub API 地址（通常端口 `8765`）。
2. Human issuer 地址（通常端口 `8766`）。
3. 一条仍有效的团队邀请链接。

然后在**自己的机器和浏览器**完成：

1. 按上文安装 Cockpit，并确保本机有一个正在运行、已注册 Agent Mail 身份的
   herdr Session。
2. 打开「设置 → 团队 Hub 连接」，填写 Team Hub API 与 Human issuer 并保存。
   内网明文 HTTP 只适用于受信任网络；跨公网必须使用 HTTPS 或 VPN。
3. 打开管理员发来的邀请链接，自行设置用户名和密码。管理员批准后登录。
4. 回到群聊左侧 Team 区域，打开对应 Topic，点击「绑定」，选择本机同项目的
   ready Session。
5. 发送消息。`auto` 会由绑定 Session 的唯一 Lead 自动处理并回复；`confirm`
   会先在每条收到的消息下等待 Human 点「让 Lead 回复」。

Team Topic 不会远控其他成员的机器，也不会把远端正文直接注入任意 pane。找不到
可绑定 Session 时，先确认 Session 正在运行、工作目录/Agent Mail project 一致，
且存在唯一 Lead 身份。

## 管理员：最小团队流程

1. 部署一个团队共享的
   [mcp_agent_mail](https://github.com/fyc0451/mcp_agent_mail) Team Hub 与 Human
   issuer。Cockpit 的本机 Hub 安装器不等于公网团队服务。
2. 每位成员在自己的 Cockpit「设置 → 团队 Hub 连接」填写相同的两个地址。
3. 在 `/#/team` 创建 Topic，生成邀请链接并发给真实成员。
4. 成员注册后，管理员点一次「批准加入」；成员随后登录并绑定自己的 Session。
5. 为 Topic 选择 `auto` 或 `confirm` 回复策略。要更换负责人时使用「改绑」，
   不要在单条消息上任意指定其他 pane。

完整的安装、角色、Agent Mail、Team 收件处理和故障排查见
[用户手册](docs/USER-GUIDE.md)。

## 配置

环境变量见 `.env.example`:

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COCKPIT_HOST` | `127.0.0.1` | 绑定地址 |
| `COCKPIT_PORT` | `8790` | 端口 |
| `COCKPIT_TOKEN` | 空 | 共享登录 token;非回环绑定时必填 |
| `HERDR_BIN` | 自动探测 | herdr 二进制路径 |
| `CODEX_BIN` | 自动探测 | codex 二进制路径 |
| `AGENT_MAIL_DB_PATH` | 自动探测 | Agent Mail `storage.sqlite3` 自定义路径 |
| `COCKPIT_VAPID_SUBJECT` | `mailto:agent-cockpit@localhost` | Web Push VAPID contact |
| `COCKPIT_VAPID_PRIVATE_KEY` / `PUBLIC_KEY` | 自动生成 | 多实例部署时可固定 VAPID 密钥对 |

Agent Mail 数据库会依次探测新版 `~/.local/share/mcp_agent_mail/` 和旧版
`~/mcp_agent_mail/`。hub token 自动从 `~/.agent-mail/client.env` 读取,不要硬编码。
VAPID 密钥首次生成于 `~/dashboard-data/`,不会进入仓库。
用户设置存在 `~/dashboard-data/settings.json`,终端字体等本机偏好存在浏览器
localStorage。session 的 Agent Mail 通信项目绑定存在
`~/dashboard-data/mail-projects.json`，只含 session、目录和项目路径，不含身份 Token。

## 升级、诊断、卸载

```bash
cd /path/to/agent-cockpit
./upgrade.sh       # 拉取当前上游、安装、构建、重启、health gate；失败自动回滚
./doctor.sh        # 检查 Python、依赖、herdr、Agent Mail、认证、服务
./uninstall.sh     # 只删 user service;代码、配置、数据保留
```

`upgrade.sh` 只接受干净的 tracked 工作区和可快进的当前分支；本地有未提交修改、
本地提交领先或分叉时会拒绝升级，不会覆盖用户工作。升级失败会切回原提交、重装
旧版并重新检查服务。未跟踪文件不会主动删除，但若与上游文件冲突，Git 会安全停止。

该脚本面向版本尚不稳定阶段的源码安装。发布者向 `origin/main` 合并候选仍走
`release_lane.py`；旧 V1 Web 升级 API 仍保持退役。

常用服务检查：

```bash
# Linux / WSL
systemctl --user status agent-cockpit.service
journalctl --user -u agent-cockpit.service -n 100 --no-pager

# macOS
launchctl print "gui/$(id -u)/io.github.fyc0451.agent-cockpit"
```

跑测试:`.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest -q`

## 项目结构

```
agent-cockpit/
├── scripts/dev_server.py  3.0 源码 8790 启动器（当前安装入口）
├── server.py              兼容启动入口（无 NEXT_PROFILE 时仍出旧看板）
├── source_native_migrate.py / release_lane.py  受管发布入口
├── agent_cockpit/         应用实现(服务、群聊账本、通信、升级)
├── web/                   3.0 群聊前端（build 后是 web/dist）
├── agent_mail_commands/    Agent Mail 命令实现
├── static/index.html      旧看板残留（安装入口不再启动）
├── tests/                 回归与安全测试
├── install.sh             3.0 一键安装（build web/dist + dev_server）
├── install-herdr.sh       自动安装/复用 Herdr，并补齐已安装 CLI 的集成
├── upgrade.sh             源码版一键升级与失败回滚
├── doctor.sh / uninstall.sh
├── agent-cockpit.service  3.0 systemd 单元（ExecStart=dev_server.py）
└── launchd.sh / agent-cockpit.plist  macOS LaunchAgent
```

## 为什么是驾驶舱而不是 CLI?

CLI agent(codex、kimi、qoder)各自强大却互相看不见。herdr 把它们放进可以
围观的 pane——但只能在那台机器的终端里看。Agent Cockpit 把这个本地终端视图
变成**网页驾驶舱**:躺在沙发上用手机就能看到哪个 agent 卡住等你,丢一张 bug
截图过去,让合适的 agent 接手。

## 限制

- **GUI agent(如 ZCode Desktop)无法上看板** — 本驾驶舱驱动的是 herdr 下的
  *终端* CLI agent,GUI 应用没有可编程控制面。
- **本机共享 token 认证** — 只保护单台 Cockpit 的局域网入口，适合可信的个人
  局域网/VPN；Team Topic 的 Human 账号由独立 issuer 管理。两者都应放在防火墙、
  VPN 或 HTTPS 之后。
- **传输安全** — HTTP 不保护会话 cookie,离开完全可信的网络请上 HTTPS 或
  Tailscale Serve。
- **Agent Mail 为必需基础设施** — 只读它的 SQLite,写入一律走 hub MCP API；
  不可用时保留既有会话控制，但禁止新增工作区或 Agent。

## 贡献与安全

开发说明见 [CONTRIBUTING.md](CONTRIBUTING.md);漏洞请按
[SECURITY.md](SECURITY.md) 私下报告(含部署威胁模型)。社区行为准则见
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## License

[MIT](LICENSE)
