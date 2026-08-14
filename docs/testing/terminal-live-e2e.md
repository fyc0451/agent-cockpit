# TERM-003 live / E2E（ordinary）

真实 Project → Workspace → Terminal 旅程的 production-shaped ordinary harness。  
不接 `8790` / `18790`，不伪造后端，浏览器不得提交 `cwd` / `command` / `PID` / `env` / Herdr 标识。

## 启动形状

`scripts/terminal_live_e2e.py`：

1. 创建调用方拥有的空 `0700` runtime root（`data` / `config` / `state` / `uploads` 由 ephemeral launcher 布局）。
2. `exec` `scripts/next_ephemeral_server.py`：随机 `127.0.0.1:0`，拒绝保留端口。
3. 等待 `GET /health/ephemeral`；descriptor 只有 `schema_version/state/base_url/pid/ready_path/ready_token`。
4. 在 ephemeral `HOME` 内种一个真实 git 目录 `term003-live-seed`（只给 discovery 列出；路径不进浏览器）。
5. 确认 Registry `items == []` 后再跑 Playwright。
6. 失败时保留 artifact（descriptor、ready、Playwright 截图/trace、server stderr），并只向本进程组发信号清理。

需要先有 `web/dist`（Next 在 ephemeral profile 下从这里出首页）。没有则 harness 先 `npm --prefix web run build`。

## 命令

```bash
# ordinary 自检：启动、健康、空 Registry、清理
python3 scripts/terminal_live_e2e.py --self-check

# 完整 ordinary live E2E（desktop 1280 + 390px）
python3 scripts/terminal_live_e2e.py --keep-on-fail
```

缺 FastAPI 时脚本会自动 `uv run --isolated --no-project --with-requirements requirements-dev.txt` 再执行，保证 ephemeral `exec` 用的是同一解释器。

不要直接 `npx playwright test -c web/playwright.config.ts`：那条配置是 fixture stub，不是 live。  
不要把 `PLAYWRIGHT_LIVE_BASE_URL` 指到 8790/18790。

## 旅程

| 步 | ordinary 期望 | 当前依赖 |
| --- | --- | --- |
| 空 Registry | `/#/projects` 显示「还没有项目」 | 已可跑 |
| 打开向导 | live `/api/runtime-nodes` 给出 local | 已可跑 |
| 登记 Project | 点选 `term003-live-seed`，不提交绝对路径 | discovery root allowlist / 向导 fingerprint |
| 创建 Workspace | workbench「创建 Workspace」 | RepoLocation gate、Web workbench |
| 打开 Terminal | 深链或卡片 | `terminal.pty` server 权威 |
| 输入 / 观察输出 | 真实 PTY | **Web writer**：`terminal.control.ui` 现为 W1 恒 false，stdin 关闭 |
| resize | FitAddon + 服务端 resize | 同上 |
| reload / reconnect / replay | 重连按钮 | 同上 |
| kill / 中断 | 中断/重启按钮 | 同上 |

当前 `web/pages/TerminalPage.tsx` 是 W1 外壳：xterm 只读、不连 WebSocket、控制按钮 disabled。live spec 把这些断言为 fail-closed，而不是伪造输出。

## 敏感边界

本车不跑 race / poison / TOCTOU / Host-Origin / FD 伪造。那些仍归 Kimi / sensitive gate。  
本车不改 Web 产品实现、后端、Delivery、服务。
