# TERM-003 live / E2E（ordinary）

真实 Project → Workspace → Terminal 旅程的 production-shaped ordinary harness。
不接 `8790` / `18790`，不伪造后端，浏览器不得提交 `cwd` / `command` / `PID` / `env` / Herdr 标识。
Selectors 锁在 Web exact `720888fb320d284aa386aadb8e4a3f5e5f7f3265`。
本提交不是产品 accepted；完整 live 由 Lead 在临时合并树跑。

## 启动形状

`scripts/terminal_live_e2e.py`：

1. 创建调用方拥有的空 `0700` runtime root（`data` / `config` / `state` / `uploads` 由 ephemeral launcher 布局）。
2. `run_suite` 先校验 provenance，再 **删除** `web/dist` 并 clean build；禁止复用 stale dist。
3. `exec` `scripts/next_ephemeral_server.py`：随机 `127.0.0.1:0`，拒绝保留端口。
4. 等待 `GET /health/ephemeral`；descriptor 只有 `schema_version/state/base_url/pid/ready_path/ready_token`。`run_suite` 与 `--self-check` 都要求 `ready === true`。
5. 在 runner-owned、discovery allowlist 已有的 `uploads` 根下种真实 git 目录 `term003-live-seed`（只给 wizard 选 public descriptor `uploads`；绝对路径不进浏览器）。不要种在 ephemeral HOME，HOME 不在 allowlist。
6. 确认 Registry `items == []` 后再跑 Playwright。
7. 失败时先对自建 pgid 发信号并 `communicate`，再落 artifact（含 `provenance.json`、`e2e-diagnostics.json`、`server.stderr.log`）。禁止对仍运行的 server 做阻塞 `stderr.read`。

Lead 临时合并树必须是 Lead exact + Web exact `720888f` + 本 live exact，不要把 Web 文件拷进本分支。

## Provenance

`run_suite` fail-closed 条件：

| 项 | 规则 |
| --- | --- |
| Web exact | `TERM003_WEB_EXACT` 缺省为 `720888fb320d284aa386aadb8e4a3f5e5f7f3265`；设成别的值直接失败 |
| Web blobs | 工作区 `web/pages/TerminalPage.tsx` 与 `web/api/terminals.ts` 必须等于该 commit 的 blob |
| Lead exact | 必须提供 `TERM003_LEAD_EXACT`（40 位 hex） |
| E2E head | 记录 `git rev-parse HEAD` 与四文件 SHA-256 |
| Bundle | clean build 后记录 `web/dist/**` SHA-256；缺 `index.html` 失败 |

`--self-check` 记录声明中的 Web/E2E 信息，但不校验 Web blob、不 build、不要求 Lead exact。

```bash
TERM003_LEAD_EXACT=<40-hex> python3 scripts/terminal_live_e2e.py --keep-on-fail
python3 scripts/terminal_live_e2e.py --self-check
```

不要直接 `npx playwright test -c web/playwright.config.ts`。
不要把 `PLAYWRIGHT_LIVE_BASE_URL` 指到 8790/18790。

## 状态隔离

一个 Playwright project、一个 ephemeral server、一条串行旅程。
空 Registry 只在任何写之前检查，并在 1280 与 390 各验一次。
写之后的 390 检查发生在同一已填充终端状态上。

## 已对齐的选择器（720888f）

| 动作 | 稳定 selector |
| --- | --- |
| discovery root | `role=button` name=`/^uploads$/i`（runner-owned allowlist root） |
| 创建 | `role=button` name=`新终端` |
| 中断 | `role=button` name=`中断`（仅 `流=live` 可点） |
| 重连 | `role=button` name=`重连`（仅 `流=reconnecting` 可点） |
| 重启 | `role=button` name=`重启` |
| 进入全屏 | `role=button` name=`全屏` `exact: true`；click 前 overlay `count === 0` |
| 全屏 overlay | `.terminal-fullscreen`（720888f；不是 `data-testid=terminal-fullscreen-overlay`） |
| 退出全屏 | overlay 内 `role=button` name=`退出全屏` `exact: true`；另覆盖 `Escape` |
| 关视图 | `role=button` name=`关闭标签页`（**全部** terminal POST 精确为 0） |
| 关会话 | `role=button` name=`关闭会话`：第一次只出「确认关闭会话？」，确认后再 POST `/close` |
| surface | `data-testid=terminal-surface-{ticketId}`；不可用外壳才是 `terminal-surface` |
| tabs | `data-testid=terminal-tabs` / `terminal-tab-{id}` |
| 流状态 | `data-testid=terminal-runtime-state` 含 `runtime=running`、`流=live`、`generation=`、`revision=` |

旅程：create → 等 live → 输入 `printf TERM003-LIVE` → 观察输出 → 全屏（按钮退出 + Escape）→ 390 resize frame / 权威 cols/rows 变化后再输入 `TERM003-390`，且 390 全屏退出路径可见可点、主容器无横向溢出/文字遮挡 → reload 后同一 ticket 回放旧 marker、无新 create → 中断成功响应 + fence 变化 + 重挂后 input/output → 重启成功响应 + generation 前进 + 重挂后 input/output → 关闭标签页零 terminal POST → 再点 tab 接管 → 关闭会话。

`requestfailed`（`/api/` 或 `terminal-tickets`）与非成功 terminal response fail-closed；诊断写入 artifact `e2e-diagnostics.json`。

## 完整 live 仍只依赖这些行为

- server `terminal.pty` 在新建 Workspace 上为 true（否则卡片 disabled / 「PTY 未接通」直接失败）
- create 后 `流=live`（attach + replay_complete）
- xterm 把 marker 暴露到可读取文本（当前默认 canvas；`.xterm-rows` / 可见文本若仍空，需 Web 补 a11y 或稳定 readout）
- reload 后自动 replay 到 live，或进入 `终端流已断开` 且「重连」可点；旧 `TERM003-LIVE` 必须再次可见，且不得 POST 新的 `/terminal-tickets`
- 中断/重启必须 2xx，推进 `generation`/`revision`，重挂后仍能 input/output
- 「关闭标签页」零 terminal POST；二次「关闭会话」恰好一次 `/close`
- 390 必须发出 `type=resize` 且 cols/rows 相对桌面变化，然后再跑一轮 input/output
- 全屏 overlay 提供可点的「退出全屏」，Escape 同样退出；390 下该按钮完全落在视口内且主容器不横溢/不遮字

## 敏感边界

本车不跑 race / poison / TOCTOU / Host-Origin / FD 伪造。
本车不改 Web 产品实现、后端、Delivery、服务。
完整 ordinary live 只在 Lead 临时集成 exact 上跑。
