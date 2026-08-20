# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> 跑在浏览器里的 CLI 编码 agent 驾驶舱:配合 [herdr](https://herdr.dev) 使用,
> 一眼看清每个 agent 的状态,随时接管终端、发指令、传截图、跨 agent 协作——
> 电脑和手机浏览器都能用。

[English](README.en.md) | [日本語](README.ja.md)

## 当前版本：Cockpit 3.0

当前产品是 **3.0 群聊**，跑在本机 `http://127.0.0.1:8790/#/chat`。
界面是瀑布流 + 成员栏 + 输入框，不是旧看板。产品线只有 3.0 和规划中的 4.0，
没有单独的 2.0 / 3.5 安装入口。

当前入口就是 `install.sh`：编译 `web/dist`，用 `scripts/dev_server.py` 起
3.0 群聊。旧看板不再作为安装结果。

## 安装 3.0

和 herdr 装在同一台机器上。需要：

| 依赖 | 用途 |
| --- | --- |
| [herdr](https://herdr.dev) | agent 所在的终端会话 |
| Git、Python 3.12+、Node.js 20+（含 npm） | 拉代码、跑服务、编译 `web/dist` |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) Hub（`:8765`） | 身份和群聊投递；没有 Hub 不能建工作区 |
| 至少一个已登录的 Agent CLI | Codex / Claude / Kimi / OpenCode / Grok / Qoder CLI CN |

仓库可以 clone 到任意路径。不必建 `$HOME/github`。发现根默认是仓库的上一级
目录；clone 就在 Home 下一层时，用仓库自己。要扫别的代码目录时再设
`COCKPIT_PROJECT_ROOT`（必须是已存在的真实目录，不能是 Home 本身）。

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

安装器会克隆到 `~/agent-cockpit`（已有 checkout 就在原地装）、建 venv、装
Agent Mail、编译 `web/dist`，并注册 `agent-cockpit.service`（macOS 为
LaunchAgent）。启动后打开 `http://127.0.0.1:8790/#/chat`。

已经有可用的 Agent Mail Hub 时会复用，不会覆盖手工/远程的
`~/.agent-mail/client.env`。跳过本机 Hub 时设 `AGENT_MAIL_SKIP_HUB=1`。
启动失败先跑 `./doctor.sh`。8790 被占用时先停掉占用该口的进程，不要改端口。

不要直接 `.venv/bin/python server.py`：没有 `COCKPIT_NEXT_PROFILE=dev` 时
首页不是 3.0。服务单元已经走 `scripts/dev_server.py`。

手动安装（不注册 systemd 时）同样要编译前端并用启动器：

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

### 不要用这些入口

| 入口 | 实际结果 |
| --- | --- |
| `scripts/next_dev.py` / `:18790` | 已冻结的 Next 2.0 隔离预览，不是当前 3.0 |
| `./upgrade.sh` | 已退役（fail-closed） |
| 打开 GitHub Latest 的 native V2 | 会把源码 8790 换成打包单元 |

源码 8790 装好之后，设置页有「一键升级」：拉官方 tag、重建 `web/dist`、重启
源码单元。不要打开 `COCKPIT_UPGRADE_V2_ENABLED`。

## 功能一览

- **群聊瀑布流** — herdr 里的 CLI agent 作为成员出现；结论进气泡，过程可展开。
- **排队发送（默认）** — Enter 排队，等对方空闲再投；要停手头工作才点「打断」。
- **Harvest** — 只在 pane `idle` / `done` 时收结论；busy 时不改上一条气泡。
- **文件与附件** — 群聊里看仓库、复制路径；附件默认折叠。
- **设置** — 外观、源码一键升级、环境自检。
- **移动端** — Hash 路由，手机浏览器可打开同一群聊。

## 工作原理

```
浏览器(电脑 / 手机)
    │  局域网 / VPN(:8790)
    ▼
Agent Cockpit(FastAPI,与 herdr 同机)
    ├── 可选只读本机 Agent Mail SQLite(WAL 模式)
    ├── 经本机或远程 Agent Mail hub MCP 写入
    ├── 读 herdr socket(所有 session)(pane 状态 / 输出)
    └── 经 SSE 向浏览器推送状态与 diff
```

部署在**和 herdr 同一台机器**上;电脑和手机只是浏览器客户端。Agent Mail Hub
可以部署在同机，也可以使用团队共享的远程服务；远程模式没有本机 SQLite 时，
本地消息列表会降级，但注册身份、创建工作区和添加 Agent 仍可正常使用。
Agent Mail 是新建工作区和添加 Agent 的前置条件；hub 暂时挂掉时禁止新增，已有消息只读，
群聊里已有气泡和 herdr pane 仍可查看。

## 首次使用

1. 确认 herdr 已在跑，并且至少有一个已登录的 agent pane。
2. 浏览器打开 `http://127.0.0.1:8790/#/chat`。
3. 左侧选工作区 / herdr session；右侧成员栏能看到花名。
4. 输入框默认是 **排队**。`@` 成员后回车，对方空闲才会开始做。
   只有要停手头工作时才点 **打断**。
5. 回复先进瀑布流。长过程折在「展开过程」里，结论在气泡里。
6. 设置页可改外观、跑环境自检、给源码 8790 做一键升级。

Hash 路由：群聊 `/#/chat`，设置 `/#/settings`。旧路径会落到群聊。

旧看板只作为仓库里的 `static/index.html` 残留；3.0 安装入口不再启动它。
那份界面的手册见 [docs/USER-GUIDE.md](docs/USER-GUIDE.md)。

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
./upgrade.sh       # 已退役(fail-closed)：一键升级引擎停用，升级走受管人工发布
./doctor.sh        # 检查 Python、依赖、herdr、Agent Mail、认证、服务
./uninstall.sh     # 只删 user service;代码、配置、数据保留
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
├── upgrade.sh             已退役
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
- **共享 token 认证** — 适合可信的个人局域网/VPN,不是多用户授权体系;
  请放在防火墙或私有网络之后。
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
