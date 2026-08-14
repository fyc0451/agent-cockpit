# TERM-003 live / E2E（ordinary）

真实 Project → Workspace → Terminal 旅程的 production-shaped ordinary harness。
不接 `8790` / `18790`，不伪造后端，浏览器不得提交 `cwd` / `command` / `PID` / `env` / Herdr 标识。
本骨架 **不是** accepted 证据。

## 启动形状

`scripts/terminal_live_e2e.py`：

1. 创建调用方拥有的空 `0700` runtime root（`data` / `config` / `state` / `uploads` 由 ephemeral launcher 布局）。
2. `exec` `scripts/next_ephemeral_server.py`：随机 `127.0.0.1:0`，拒绝保留端口。
3. 等待 `GET /health/ephemeral`；descriptor 只有 `schema_version/state/base_url/pid/ready_path/ready_token`。
4. 在 ephemeral `HOME` 内种一个真实 git 目录 `term003-live-seed`（只给 discovery 列出；路径不进浏览器）。
5. 确认 Registry `items == []` 后再跑 Playwright。
6. 失败时先对自建 pgid 发信号并 `communicate`，再落 artifact（descriptor、ready、Playwright 截图/trace、server stderr）。禁止对仍运行的 server 做阻塞 `stderr.read`。

需要先有 `web/dist`（Next 在 ephemeral profile 下从这里出首页）。没有则 harness 先 `npm --prefix web run build`。

## 命令

```bash
# ordinary 自检：启动、健康、空 Registry、清理
python3 scripts/terminal_live_e2e.py --self-check

# 完整 ordinary live E2E（同一旅程内验证 1280 与 390px）
python3 scripts/terminal_live_e2e.py --keep-on-fail
```

缺 FastAPI 时脚本会自动 `uv run --isolated --no-project --with-requirements requirements-dev.txt` 再执行，保证 ephemeral `exec` 用的是同一解释器。

不要直接 `npx playwright test -c web/playwright.config.ts`：那条配置是 fixture stub，不是 live。
不要把 `PLAYWRIGHT_LIVE_BASE_URL` 指到 8790/18790。

## 状态隔离

一个 Playwright project、一个 ephemeral server、一条串行旅程。
空 Registry 只在任何写之前检查，并在 1280 与 390 各验一次。
写之后的 390 检查发生在同一已填充状态上，不再要求 empty。
缺 Project / Workspace / TERM-003 控件必须失败，禁止 `annotation blocked` 后 return。

## 旅程

| 步 | ordinary 期望 | 当前缺口 |
| --- | --- | --- |
| 空 Registry | `/#/projects` 显示「还没有项目」 | self-check 已覆盖后端空集 |
| 打开向导 | live `/api/runtime-nodes` 给出 local | 依赖 live discovery |
| 登记 Project | 点选 `term003-live-seed`，不提交绝对路径 | discovery root allowlist / fingerprint |
| 创建 Workspace | workbench「创建 Workspace」可用 | RepoLocation gate |
| 打开 Terminal | 卡片可点进 `/terminal` | `terminal.pty` 仍可能 deferred |
| 启动 / 输入稳定命令 / 观察输出 | 真实 PTY | **Kimi API + accessible names 未提交** |
| resize | 390 后 surface 仍可见且宽度变化 | 同上 |
| reload / reconnect / replay | 重连可用 | 同上 |
| interrupt / restart | 中断、重启可用 | 同上 |
| 离开视图 | 不得再 POST terminal | 同上 |
| 显式关闭 | 「关闭」可用 | 同上 |

W1 只读外壳（按钮 disabled、零 WebSocket）**不是** TERM-003 acceptance。Kimi 控件名提交后，在临时合并树把本 spec 接到真实 create→input→output→resize→reload/reconnect/replay→interrupt/restart→close-view 零 POST→显式 close。

## 敏感边界

本车不跑 race / poison / TOCTOU / Host-Origin / FD 伪造。那些仍归 Kimi / sensitive gate。
本车不改 Web 产品实现、后端、Delivery、服务。
