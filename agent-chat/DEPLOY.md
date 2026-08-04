# agent-chat · M1a 部署与验证（Hub 内网可达 + 跨机 send/fetch/ack）

> 设计依据：`DESIGN.md`（方案 Y）；ADR `2026-08-04-agent-cockpit-team-channel.md`
> 本阶段只做一件事：让 Hub 在内网可达，验证两机 agent 用现有 `mail-send` / `mail-recv` 跨机收发、认领、确认全链路。

## 1. 角色

| 角色 | 说明 |
|---|---|
| 团队服务器 | 内网一台机器，部署 Hub（外部开源 `mcp_agent_mail`），监听内网 `:8765` |
| 开发者机器 | 各自本机跑 cockpit + agent；`client.env` 指向团队服务器 Hub |

## 2. 服务器侧：部署 Hub（监听内网）

上游 `mcp_agent_mail` 自带部署资产（systemd / Docker / scripts），按上游文档安装，要点：

```bash
# 上游仓库的 systemd 示例即以 uvicorn 监听 0.0.0.0:8765（内网可达）
#   ExecStart=/usr/bin/uvicorn mcp_agent_mail.http:build_http_app --factory --host 0.0.0.0 --port 8765
# token 通过环境变量配置：HTTP_BEARER_TOKEN=<团队共享 token>
# 具体安装步骤以 https://github.com/Dicklesworthstone/mcp_agent_mail 文档为准
```

部署后自检：

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer <token>" http://<服务器>:8765/mcp/
# 期望：401/200 均说明端口可达；再在另一台机用 mail-send 验证真正连通
```

安全：内网部署，不暴露公网；服务器**持有并校验 token**，不存 agent 私钥/凭据；beta 用团队共享 token，per-agent token 见 M1b。

## 3. 开发者机器侧：client.env 指向服务器

每台开发者机器（一次性）：

```bash
cat > ~/.agent-mail/client.env <<'EOF'
hub=http://<服务器>:8765
token=<团队共享 token>
EOF
```

- `mail-send` / `mail-recv` / `am-register` 通过 `am_common.load_client_config()` 读该文件，自动走服务器；
- cockpit 的写操作（`hub_client.py`）同样读该文件——本次已修复其"硬编码 127.0.0.1"问题，尊重 `client.env` 的 hub 字段；个人模式（hub=127.0.0.1 或未配置）行为不变。

## 4. 身份注册（花名唯一）

每个 agent 实例在各自机器注册一次：

```bash
am-register --agent <program> --instance <instance> --project <项目绝对路径> \
  --name <团队唯一花名> --program <program>
```

- 花名团队内唯一（M1a 手工约定；唯一性校验与存量迁移在 M1b）；
- `set_contact_policy=open` 自动设置，他人才能 @。

## 5. M1a 验收（两机跨机全链路）

在机器 A（如 codex-main）与机器 B（如 opencode-main）上：

```bash
# A → B
mail-send --agent codex --instance main --project <path> \
  --to <B 的花名> --subject "M1a 跨机测试" --body "hello from A"

# B 侧收到
mail-recv --agent <B程序> --instance <实例> --project <path> --unread

# B 处理并回执（claim → complete，完整链路）
mail-recv ... --message <id>          # claim
mail-recv ... --complete <id> --claim-token <token>
```

验收标准：

- A 发送成功，B 的 `mail-recv --unread` 能读到该消息（跨机投递 ✓）；
- B claim / complete 走本地 sidecar，且 ack 回服务器（跨机回执 ✓）；
- B 反向发回 A，A 能读到（双向 ✓）。

## 6. 回归

- 个人模式不受影响：`client.env` 为 `127.0.0.1` 时，`hub_client.py` 与 mail 工具行为与改动前一致；
- 运行 `python3 -m pytest tests/test_hub_client.py`（本次新增 hub 地址解析覆盖在 `tests/`）；
- 全量测试见 CI 六矩阵。

## 7. M1a 范围外（后续里程碑）

- 频道 fanout / @ durable 收件箱 / 身份目录 / 在线状态 / Web 看板 → M1b/M2/M3；
- per-agent token、花名唯一性校验与存量迁移、收件箱合并语义 → M1b；
- cockpit 读侧（`db.py` 本机直读）跨机 → 看板/待办跨机阶段。
