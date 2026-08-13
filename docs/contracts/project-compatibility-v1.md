# Project Compatibility Contract v1

`PROJ-005-compatibility` 只把已有 legacy workbench 读模型绑定到 Project
Registry identity。它不迁移 legacy authority，不创建 Workspace，也不改变
Project Registry API 或 legacy API 的响应信封。

## API boundary

- 只调整已有 `GET /api/projects/{slug}/workbench` 的内部只读解析。
- 成功响应仍严格只有 `project`、`assignments`、`sessions`、`source` 四键。
- `project` 仍返回 legacy Agent Mail 的整数 `id`、`slug`、`created_at`。
- legacy 鉴权和 `{detail: ...}` 错误信封保持不变。
- Agent Mail 不可用仍固定为脱敏 503 `Agent Mail 不可用`，查询异常固定为
  脱敏 503 `Agent Mail 查询失败`；provider reason/异常正文不得透传。未知 legacy slug 仍为 404；
  Coordination 查询失败仍为 503。
- 不新增 G3、Workspace、Project 或 session API，不把 workbench 纳入 Registry
  API 的 G3 exception bridge。

URL 中的 `slug` 只能传给 Registry 的 `get_project_by_slug`，并由返回记录取得
opaque `project_id`。`slug` 与 `project_id` 不得互换，也不得用同名字符串推断
identity。

## Canonical provenance

兼容读取必须调用 Store-owned read primitive
`RegistryStore.list_legacy_bindings(project_id)`，不得直接查询 Registry SQL。

`source_key` 与 `project_legacy_import` 使用完全相同的 canonical 规则：

```text
canonical_json = JSON(sort_keys=true, separators=(",", ":"), ensure_ascii=true,
                      allow_nan=false)
source_key = "sha256:" + SHA256_ASCII(canonical_json(identity))
```

Agent Mail Project identity 是：

```json
{"project_id": 7}
```

其 source kind 必须是 `agent_mail_project`。当前 Registry Project 必须恰好有一条
`agent_mail_project` binding，且 source key 必须等于当前 legacy Agent Mail 整数
project ID 的 canonical key。缺失、额外冲突或不匹配均 fail closed。

Live Herdr session identity 是完整 generation pair：

```json
{"session": "target", "session_dir": "/sessions/target"}
```

只有同时满足下列条件的 snapshot session 才可进入响应：

1. `mail_projects.get(session, session_dir)` 精确返回当前 legacy `human_key`；
2. 当前 Registry Project 的 accepted session provenance binding 包含该完整 pair
   的 canonical source key。

accepted session provenance source kind 是严格闭集
`mail_projects_session | herdr_session`。两者都由 importer 以相同完整
`{"session","session_dir"}` identity 生成；任一 accepted kind 的 exact key 可证明
imported generation provenance。`coordination_run`、`agent_mail_project` 或任何未来/未知
kind 即使 source key 字节相同也不能证明 live session。

session 名、slug、Pane cwd、title 或 Agent 名均不能证明归属。同名 session 的
`session_dir` 不同即为不同 generation。绑定到其他 legacy Project 或没有 exact
live binding 的 session 直接忽略；已经由 live binding 认领到当前 Project、但
Registry exact provenance 缺失或不匹配时，整个请求 fail closed。

Registry Project、binding snapshot、Agent Mail Project binding 或已认领 session
provenance 无法证明时，固定返回：

```text
HTTP 409
{"detail":"项目兼容绑定冲突"}
```

detail 和日志不得回显 path、SQL、source key、project ID 或 provider 异常正文。

## Herdr degradation

Herdr unavailable、degraded、非 object 或 snapshot read exception 仍返回 200，
`sessions=[]`，并设置 `source.degraded=true`。可用且非 degraded 的 snapshot 才逐项
应用 exact live binding 与 Registry provenance gate。adapter 不读取 persisted
Herdr `session.json`。

`sessions` 不是 list/tuple 时同样返回200降级空列表，不能保留
`available=true,degraded=false`。同一 snapshot 中重复的 exact
`(session,directory)` generation 固定返回脱敏409，不能重复 append或依赖输入顺序选一条。

## Read-only boundary

adapter 和 route 只能调用下列 read providers：

- Agent Mail status 与 `project_by_slug`；
- Registry `get_project_by_slug` 与 `list_legacy_bindings`；
- Coordination `list_assignments`；
- live Herdr snapshot；
- live `mail_projects.get(session, session_dir)`。

server 冷缓存不得调用 Registry `initialize`。已有 `_project_registry_store` 可复用；
没有缓存时必须只调用
`project_registry_store.open_existing(runtime_paths.store("project_registry"))`，且不把
该 route 的只读 handle写入全局缓存。Registry 缺失/损坏统一映射为固定脱敏409，
不得创建 DB、DDL、migration、WAL/SHM、临时文件或执行 fsync。

`mail_projects.get` 的任何异常也统一映射为同一固定脱敏409，不回显 provider异常。
Mail status非 Mapping或读取异常固定为 `Agent Mail 查询失败` 503。Assignment rows
不可迭代或任一 row非 Mapping固定为 `Coordination 查询失败` 503。legacy Project缺少
整数 `id`、匹配slug或非空 `human_key`，以及 Registry record缺少匹配slug/opaque
`project_id`，均固定为脱敏409。

禁止调用 Registry create/import/bind/idempotency mutation，禁止写 Agent Mail、
mail-projects、Coordination、Herdr、Store、Git 或文件，禁止启动/发送 Agent、Pane、
prompt 或 dispatch。兼容读取不得解析 persisted Herdr，不得直查 Registry SQL。

## Field allowlists

assignment、session 和 pane 继续使用现有 allowlist。响应不得包含 `human_key`、
session directory、cwd、token、title、mail name、agent session、Registry opaque ID、
source key、SQL 或 provider error。
