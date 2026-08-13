# Project Registry v1

## 边界

Project Registry 是独立的 app-owned SQLite Store。v1 只提供 schema、领域校验和以下持久化
primitive：创建 Project、登记 RepoLocation、创建 Workspace identity、写入 legacy provenance、
以及带持久幂等账的 Project 创建。

本 car 不扫描或导入 legacy authority，不发现目录，不提供 HTTP API，也不执行 archive、restore、
detach、transfer、Workspace plan/execute、Git、Herdr、Mail 或文件系统副作用。表中 lifecycle 字段
只冻结持久状态与数据库不变量，不代表这些 lifecycle 操作已经公开。

## Schema 与打开语义

- `SCHEMA_VERSION = 1`，migration ID 为 `project-registry-v1`。
- `schema_migrations` 是 DDL ledger；`legacy_project_bindings` 是 provenance ledger，两者不得混用。
  两者与 `idempotency_records` 都由数据库 trigger 保证 append-only，任何 `UPDATE`/`DELETE` 均拒绝。
- 空路径只能由显式 `initialize(path)` 创建 v1 schema；既有非 v1 Store 不做推测修补或破坏性迁移。
- `open_existing(path)` 是纯读验证：不创建、不 migration、不启用 WAL、不留下 `-wal/-shm`。
- future version 返回 `future_schema`；旧 version 返回 `migration_required`；当前 version 的 table、
  trigger、index、FK、migration receipt 或 DDL fingerprint 不一致返回
  `schema_fingerprint_mismatch`。
- 初始化的 DDL、migration receipt、`PRAGMA user_version=1` 和校验处于同一事务；失败全部回滚。
- 首次初始化先在目标同目录的私有随机 temp 中完成事务、schema 校验和 file `fsync`，再以
  no-replace 原子发布（Linux/WSL `renameat2(RENAME_NOREPLACE)`，macOS
  `renamex_np(RENAME_EXCL)`）并 `fsync` 父目录；最终路径从不暴露半初始化 DB，并发初始化复用先
  发布的完整 winner。预存或新出现的 `-journal/-wal/-shm` 一律 fail closed，不删除或改写。失败路径不对
  已发布为 pathname 的 temp 执行有 TOCTOU 风险的清理；128-bit 随机、`0600` 的私有 temp 可以
  遗留，但运行时不扫描、复用或纳入 Registry catalog，也绝不因此删除并发 replacement。残留只可
  由后续受信任、具备独立安全判定的 maintenance 流程处理。
- 每个写连接启用 `PRAGMA foreign_keys=ON`，写事务使用 `BEGIN IMMEDIATE`；Store 文件模式为 `0600`。
- 公开 readiness fingerprint 常量在 `agent_cockpit.project_registry_contracts`：
  `PROJECT_REGISTRY_TABLES`、`PROJECT_REGISTRY_DEFAULTS`、`PROJECT_REGISTRY_INDEXES`、
  `PROJECT_REGISTRY_FOREIGN_KEYS`、`PROJECT_REGISTRY_TRIGGERS`、
  `PROJECT_REGISTRY_MIGRATION_RECEIPT`。

## Project

`project_id` 是服务端生成的 `prj_` + 128-bit random opaque ID，不由 slug 或路径派生。slug 必须是
1 到 64 字符的 normalized ASCII lower-kebab。`project_id` 与 slug 创建后不可变，并由无条件
`UNIQUE` 和禁止物理 `DELETE` 的数据库 trigger 保证 slug 全生命周期不复用。`display_name` 和
`goal` 不参与 identity。lifecycle 仅为 `active|archived`，version 从 1 开始。

## RepoLocation

`repo_location_id` 是服务端生成 opaque ID。identity 是目标 node 上 provider 已规范化的
`(node_id, canonical_path)`；Store 不对 remote path 调用本机 `Path.resolve()`，也不按 Git remote、
inode 或 basename 合并。

`repo_location_id`、owner `project_id`、`node_id` 和 `canonical_path` 创建后不可变；RepoLocation
禁止物理 `DELETE`，生命周期变更是释放 active 槽的唯一机制。

RepoLocation lifecycle 为 `active|archived`，availability 为独立观测态
`available|offline|missing|unknown`。唯一槽由 partial unique index 精确定义：

```sql
CREATE UNIQUE INDEX repo_locations_active_node_path
ON repo_locations(node_id, canonical_path)
WHERE lifecycle = 'active';
```

因此 `missing/offline` 仍占 active 槽；只有 archived row 释放。恢复时若槽已被占用，SQLite 唯一约束
拒绝恢复，不自动 merge、transfer 或复用历史 row。

## Workspace

`workspace_id` 是服务端生成 opaque ID。冗余 `project_id` 必须和 RepoLocation owner 相同；数据库以
复合外键而非两个独立 FK 保证：

```sql
FOREIGN KEY(project_id, repo_location_id)
REFERENCES repo_locations(project_id, repo_location_id)
```

Repository 对未知 location 和其他 Project 的 location 统一返回 `repo_location_not_found`，避免成为
跨 Project existence oracle。`workspace_id`、owner `project_id` 和 `repo_location_id` 创建后不可变，
Workspace 禁止物理 `DELETE`；同 Project active Workspace name 使用 partial unique index。

## Legacy provenance

唯一 authority identity 为 `(source_kind, source_key)`。source kind 区分
`agent_mail_project`、`mail_projects_session`、`herdr_session`、`coordination_run`；不同 authority
中的相同 source key 不冲突。同一 pair 仅在 project 和 digest 都相同时幂等重放，否则返回
`legacy_binding_conflict`。写 provenance 不修改任何 legacy source。
Ledger row 只允许首次插入，不允许改写或删除。

## Idempotency

幂等 identity 为 `(scope, idempotency_key)`，请求使用 canonical JSON SHA-256 digest。相同 digest
精确重放首次持久化的 status/response/opaque IDs；不同 digest 返回 `idempotency_conflict`。
aggregate mutation 和 `idempotency_records` 写入同一 `BEGIN IMMEDIATE` 事务，mutation 失败不留下
reservation，ledger 写失败也不留下 aggregate。
首次 receipt 持久化后不允许改写或删除。

## 稳定错误

所有产品错误派生自 `ProjectRegistryError`，只暴露 ASCII `code`，不泄漏 SQLite SQL、约束原文、
路径或 payload：

`schema_missing`、`migration_required`、`future_schema`、`schema_fingerprint_mismatch`、
`store_corrupt`、`store_unsafe`、`invalid_argument`、`project_not_found`、
`project_slug_conflict`、`location_already_registered`、`repo_location_not_found`、
`workspace_name_conflict`、`legacy_binding_conflict`、`idempotency_conflict`。
