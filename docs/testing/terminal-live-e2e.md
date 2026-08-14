# TERM-003 live / E2E（ordinary）

真实 Project → Workspace → Terminal 旅程的 production-shaped ordinary harness。
不接 `8790` / `18790`，不伪造后端，浏览器不得提交 `cwd` / `command` / `PID` / `env` / Herdr 标识。
旧 Web exact `eb75ace0` 已 superseded。本 live/E2E scope 独立继续；全屏退出路径依赖即将到来的 Web rework contract，新 Web exact 到站后还要对一次 selector。
本提交不是产品 accepted；完整 live 由 Lead 在临时合并树跑。

## 启动形状

`scripts/terminal_live_e2e.py`：

1. 创建调用方拥有的空 `0700` runtime root（`data` / `config` / `state` / `uploads` 由 ephemeral launcher 布局）。
2. `exec` `scripts/next_ephemeral_server.py`：随机 `127.0.0.1:0`，拒绝保留端口。
3. 等待 `GET /health/ephemeral`；descriptor 只有 `schema_version/state/base_url/pid/ready_path/ready_token`。
4. 在 ephemeral `HOME` 内种一个真实 git 目录 `term003-live-seed`（只给 discovery 列出；路径不进浏览器）。
5. 确认 Registry `items == []` 后再跑 Playwright。
6. 失败时先对自建 pgid 发信号并 `communicate`，再落 artifact。禁止对仍运行的 server 做阻塞 `stderr.read`。

需要先有 `web/dist`。没有则 harness 先 `npm --prefix web run build`。Lead 临时合并树必须用新 Web exact 再 build，不要把 Web 文件拷进本分支。

## 命令

```bash
python3 scripts/terminal_live_e2e.py --self-check
python3 scripts/terminal_live_e2e.py --keep-on-fail
```

不要直接 `npx playwright test -c web/playwright.config.ts`。
不要把 `PLAYWRIGHT_LIVE_BASE_URL` 指到 8790/18790。

## 状态隔离

一个 Playwright project、一个 ephemeral server、一条串行旅程。
空 Registry 只在任何写之前检查，并在 1280 与 390 各验一次。
写之后的 390 检查发生在同一已填充终端状态上。

## 已对齐的选择器

来源：上一版 Web exact 的终端控件名，加上即将到来的全屏 overlay contract。

| 动作 | 稳定 selector |
| --- | --- |
| 创建 | `role=button` name=`新终端` |
| 中断 | `role=button` name=`中断`（仅 `流=live` 可点） |
| 重连 | `role=button` name=`重连`（仅 `流=reconnecting` 可点） |
| 重启 | `role=button` name=`重启` |
| 进入全屏 | `role=button` name=`全屏` |
| 全屏 overlay | `data-testid=terminal-fullscreen-overlay`（Web rework contract） |
| 退出全屏 | overlay 内 `role=button` name=`退出全屏`；另覆盖 `Escape` |
| 关视图 | `role=button` name=`关闭标签页`（零 POST `/close`，tab 变 `terminal-tab--detached`） |
| 关会话 | `role=button` name=`关闭会话`：第一次只出「确认关闭会话？」，确认后再 POST `/close` |
| surface | `data-testid=terminal-surface-{ticketId}`；不可用外壳才是 `terminal-surface` |
| tabs | `data-testid=terminal-tabs` / `terminal-tab-{id}` |
| 流状态 | `data-testid=terminal-runtime-state` 含 `runtime=running` 与 `流=live` |

旅程：create → 等 live → 输入 `printf TERM003-LIVE` → 观察输出 → 全屏（按钮退出 + Escape）→ 390 resize 且 390 全屏退出路径可见可点、主容器无横向溢出/文字遮挡 → reload/replay（断流则点重连）→ 中断 → 重启 → 关闭标签页零 POST → 再点 tab 接管 → 关闭会话。

## 完整 live 仍只依赖这些行为

- server `terminal.pty` 在新建 Workspace 上为 true（否则卡片 disabled / 「PTY 未接通」直接失败）
- create 后 `流=live`（attach + replay_complete）
- xterm 把 `TERM003-LIVE` 暴露到可读取文本（当前默认 canvas；`.xterm-rows` / 可见文本若仍空，需 Web 补 a11y 或稳定 readout）
- reload 后自动 replay 到 live，或进入 `终端流已断开` 且「重连」可点
- 中断/重启 POST 后仍能回到 live
- 「关闭标签页」不发 `/close`；二次「关闭会话」恰好一次 `/close`
- 全屏 overlay 提供可点的「退出全屏」，Escape 同样退出；390 下该按钮完全落在视口内且主容器不横溢/不遮字

## 敏感边界

本车不跑 race / poison / TOCTOU / Host-Origin / FD 伪造。
本车不改 Web 产品实现、后端、Delivery、服务。
