# Scheduler Authority Contract v1

本文件是 `SCHED-001-authority-contract` 的唯一权威。它只冻结 authority、
公开 DTO、reason codes、freshness、45/90/135 时间语义、dispatch 依赖清单、
logical lease 唯一性、CAS saga/heartbeat owner，以及 fail-closed fixtures。

本车不实现 projection 运行时、timer、DB、lease store、dispatch、server、Web
或 Delivery 写入。`SCHED-002` 才实现纯函数只读投影。在本文件列出的依赖全部
accepted 之前，真实 dispatch 必须保持 `available=false`。

## 1. 范围与禁止项

允许修改的路径只有：

- `docs/contracts/scheduler-v1.md`
- `tests/test_scheduler_contract.py`
- `tests/fixtures/scheduler_v1/`

禁止：

- 任何 `agent_cockpit/` 生产 scheduler 模块
- timer / lifespan / 复用 2s/10s live poll
- Coordination / Herdr / SQLite / lease / claim / heartbeat 实现
- `server.py`、`web/`、`.delivery/`、handoff、服务

SCHED-001/002 的副作用边界：

- 不写 Delivery、Assignment、Project Registry、Attention 或任何 receipt
- 不创建、启动、停止 Agent / Pane / 进程
- 不发送 prompt、Mail、control message 或 Web Push
- 不执行 Git mutation
- `idle` / `done` 只影响投影，不得把 car / Assignment / WorkItem 标成
  review / accepted / closed
- lead 保留 owner、reviewer、accept、merge、release

## 2. 字段级四权威（禁止双向同步）

Scheduler 不是第五个工作或身份真源。四类业务权威按字段单写：

| 对象 | 唯一可写权威 | Scheduler 可持有 | 禁止复制或回写 |
| --- | --- | --- | --- |
| Delivery car | Delivery plan + 经授权的 Delivery CAS | `source_ref` / revision / digest、lease overlay | status、depends_on、scope、owner、reviewer、WIP、acceptance |
| Assignment | Coordination Assignment CAS | 可选不可变 typed reference 与 observed version | assignment 文本、assignee、status、deadline 的镜像状态机 |
| Agent identity | `WorkspaceAgentIdentity` store | identity ID/revision 引用、eligibility reason | lifecycle、role、Checkout、desired runtime、当前 work owner |
| Runtime generation | `RuntimeAttachment` + generation handshake | attachment ID/revision、generation、heartbeat evidence ref | generation、harness、node/epoch、observed state |

Assignment 的 `review` 与 Delivery 的 `review` 语义、终态和 revision 不同。
没有显式 typed link 时，即使文本与 assignee/owner 相同，也必须保留两个对象
并输出 `unlinked_authorities`，不得去重、合并或互相推进。即使存在 typed
link，Delivery `accepted` 也不得自动 close Assignment；Assignment
`in_progress` 也不得把 car 置为 `in_progress`。未来 WorkItem 成为工作权威时，
Delivery 只能作为 typed source reference，不能双向同步两个状态机。

Pane、launch descriptor、Coordination participant、task report 和 Mail 显示名
都不是身份主键，也不能代替 `runtime_generation`。

## 3. 权威输入与 fingerprint

调用方必须显式提供 authoritative Delivery reference，禁止扫描任意 cwd 寻找
`.delivery`。最小字段：

```text
repository_id
plan_path
git_head
git_dirty                 # boolean；dirty 是 revision 的一部分，不等于 invalid
plan_sha256               # sha256:<64 lowercase hex>
observed_at               # UTC，偏移必须是 +00:00，禁止 Z
```

同一 reconciliation 输入必须同时固定 Delivery、Assignment、Herdr observation
的 revision/fingerprint 和 `evaluated_at`。事件只使投影 invalid；每次计算重新
读取显式来源，不得用事件 payload 直接改业务结论。

`source.revision` 是下列字段 canonical JSON 的 SHA-256：

```text
repository_id, plan_path, git_head, git_dirty, plan_sha256
```

`git_head`、dirty marker、`plan_sha256` 任一变化都改变 source revision 与
`input_fingerprint`。仅 `evaluated_at`、age、alert duration 变化不得改变
fingerprint。authoritative location 有多个候选时必须
`authoritative_plan_ambiguous`。

`git_dirty=true` 的 revision 仍是合法且可审计的输入，但 v1 必须输出
`source_dirty`，并把 fresh source 视为不可派：不得形成 eligible pair、ready
dispatch 或 ready-without-dispatch alert。它不是 `source_invalid`，也不得被静默
当作 fresh。

### 3.1 钉死的普通金样（禁止浮动“当前 Lead Delivery”）

本车普通金样固定到 writer base，不得引用未钉死的 Lead HEAD：

```text
git_head     = a75e8c46920d57d947a4caebec379d8d54e9015e
plan_path    = .delivery/cockpit-product-v3.json
git_dirty    = false
plan_sha256  = sha256:b555dd4f36fa1ed3cdfe6ef22f338d26efdeb4a1f42d577f2f8fb202ab991c54
```

该指纹的 expected 容量与 ready 队列必须与同文件保存：

```text
capacity.effective = 3
capacity.active    = 1    # PROJ-003-legacy-import
capacity.remaining = 2
work.ready         = PROJ-004-project-api, SCHED-001-authority-contract
dispatch.available = false
```

父提交 `0bbbc561f9347649fb0901c527e6f812bac9696f` 的 plan digest 是
`sha256:50102e76d621fe3cb30bd9266929ac053dd36d7cf2bce9e24d7fef362619525c`。
它不得与 `a75e8c4` 共享 fingerprint，也不得复用 `a75e8c4` 的 ready/capacity。
父提交 expected：`effective=3, active=2, remaining=1`，ready 仅
`SCHED-001-authority-contract`（`PROJ-004` 仍等待 `PROJ-004A`）。

## 4. 公开 DTO

SCHED-002 冻结纯函数边界：

```text
project_scheduler_projection(
    snapshot: SchedulerSnapshot,
    *,
    evaluated_at: datetime,
    previous: SchedulerProjection | None = None,
) -> SchedulerProjection
```

不得读取环境变量、cwd、系统时钟、Git、SQLite、Herdr、Mail 或网络。禁止 `Any`
扩展字段。未知 source/status/reason/enum 一律 fail-closed 为 `source_invalid`，
不得透传任意字符串。

### 4.1 SchedulerSnapshot

```text
schema_version: 1
project_id: str
source: DeliverySourceSnapshot
capacity: CapacitySnapshot
work: tuple[WorkSnapshot, ...]
agents: tuple[AgentObservation, ...]
assignments: tuple[AssignmentObservation, ...]
active_leases: tuple[ActiveLeaseObservation, ...]
active_dispatch_count: int          # v1 必须精确为 0
evaluated_input_revision: str
```

`active_dispatch_count` 不是从 Coordination claim、Pane 或 Assignment 推导的。
负数或非零均为 `source_invalid`。未来 lease authority 落地必须升级合同版本。

### 4.1.1 严格输入闭集

所有 DTO（包括嵌套 `capacity`、work、agent、assignment、lease 和
`TypedAuthorityLink`）均为 exact-key object：缺字段、额外字段、未知 enum、非
list collection 或错误 strict type 都是 `source_invalid`。整数必须是 JSON integer，
boolean 不能作为整数接受。每个 `TypedAuthorityLink.kind/id` 必须指向同一 snapshot
中对应 kind 的对象；work、assignment、agent ID 必须唯一。lease 必须引用同 source
ID/kind 的 work；旧 revision lease 可以存在以表示 fencing 反例，但不能释放 logical
uniqueness。

JSON object 出现重复 key 必须在 decode 时拒绝为 `source_invalid`，不得接受最后一个值。
`source.revision` 必须等于第 3 节的 canonical digest；`evaluated_input_revision`
必须等于整个 snapshot（不含它自身）的 canonical digest。未知 `schema_version` 必须
fail-closed，不能按 v1 猜测解释。

### 4.2 DeliverySourceSnapshot

```text
repository_id: str
plan_path: str
git_head: str
git_dirty: bool
plan_sha256: str
status: available | unavailable | invalid | ambiguous
observed_at: datetime | None
freshness_deadline: datetime | None
revision: str
reason_codes: tuple[ReasonCode, ...]
```

### 4.3 CapacitySnapshot

```text
effective_writer_wip: int
active_writer_count: int
remaining_writer_capacity: int
revision: str
```

writer occupancy 使用逻辑并集，禁止双计：

```text
occupancy = count(
  Delivery active car IDs
  UNION active writer reservations whose source car is still planned/ready
)
```

`in_progress` 与 `review` 都计入 writer WIP。不得按在线 Agent 数或 reviewer 数
重算容量。effective WIP 的唯一来源是 Delivery gate：未 accepted 的 WIP3 gate
保持 2，accepted 后为 3。`remaining = effective - occupancy`。

### 4.4 WorkSnapshot

```text
source_kind: delivery_car | work_item | review_packet | assignment
source_id: str
source_revision: str
phase: writer | reviewer
state: ready | waiting | review | active | accepted | blocked
waiting_on: tuple[str, ...]
author_agent_instance_id: str | None
sensitivity: ordinary | sensitive_security | unclassified
typed_links: tuple[TypedAuthorityLink, ...]
reason_codes: tuple[ReasonCode, ...]
```

`sensitivity` 必须来自工作权威的 explicit revisioned field。标题关键词不是
权威。当前 legacy Delivery / Assignment 无该字段时必须记为 `unclassified`，
真实 dispatch 不可用。Scheduler lease row 不得冒充工作属性。

### 4.5 AgentObservation

```text
agent_instance_id: str | None
identity_revision: str | None
attachment_id: str | None
attachment_revision: str | None
runtime_generation: int | None
observed_harness: codex | claude | kimi | kimi-code | opencode | zcode | unknown
verified_harness: claude | kimi | codex | opencode | zcode | unknown | None
observed_state: working | blocked | done | idle | unknown
projected_state: available | working | blocked | paused | recovery_required | unknown_transport | unavailable
observed_at: datetime | None
freshness_deadline: datetime | None
source_revision: str
reason_codes: tuple[ReasonCode, ...]
```

`verified_harness` 只接受当前 RuntimeAttachment 经 versioned Harness Catalog
核对后的 canonical ID。`kimi-code` 只是观察别名，不是 verified catalog ID。
Pane `idle` 只是 `available` 的必要观测，绝不是充分条件。缺 identity 或
generation 时必须 `no_stable_identity` / `runtime_generation_unavailable` 或
`runtime_not_verified`，eligible count 为 0。

### 4.6 AssignmentObservation

```text
assignment_id: str
source_revision: str
status: assigned | in_progress | blocked | review | closed
assignee: str | None
typed_links: tuple[TypedAuthorityLink, ...]
```

### 4.7 ActiveLeaseObservation（只读 overlay，v1 输入只用于反例）

```text
lease_id: str
project_id: str
workspace_id: str
source_kind: str
source_id: str
source_revision: str
phase: writer | reviewer
agent_instance_id: str
status: offered | claimed_pending_source | working | compensating | terminal
```

v1 projection 不得把这些观察写成 store。它们只用于冻结 uniqueness / occupancy
反例。

### 4.8 SchedulerProjection

```text
schema_version: 1
project_id: str
input_fingerprint: str
evaluated_at: datetime
next_reconciliation_at: datetime
source: SourceProjection
capacity: CapacityProjection
work: WorkProjection
agents: AgentProjection
dispatch: DispatchProjection
alerts: tuple[SchedulerAlertIntent, ...]
reason_codes: tuple[ReasonCode, ...]
```

```text
DispatchProjection
  available: false
  mode: readonly
  reason_code: readonly_projection_only
  missing_dependencies: tuple[DispatchDependencyId, ...]
```

数组按稳定 key 排序；reason codes 去重后按字典序。相同输入与相同显式时间必须
byte-equivalent。

### 4.9 SchedulerAlertIntent

```text
kind: ready_without_dispatch | ready_but_no_eligible_agent | source_health
severity: info | warning
dedupe_key: str
status: absent | pending | open | resolved
first_observed_at: datetime | None
observed_for_seconds: int
reason_code: ReasonCode
evidence_refs: tuple[str, ...]
```

SCHED-001/002 不持久化 observation 或 alert row。无 previous / 无显式
`first_observed_at` 时只能输出 `absent` + `timing_unavailable`，不得 open。

## 5. Reason Codes v1

公开集合严格冻结为：

```text
source_unavailable
source_stale
source_dirty
source_invalid
authoritative_plan_ambiguous
dependency_waiting
writer_wip_full
scope_ownership_conflict
review_backpressure
reviewer_required
author_gate_denied
unlinked_authorities
no_stable_identity
runtime_not_verified
runtime_generation_unavailable
runtime_generation_drift
recovery_required
transport_unknown
heartbeat_expired
agent_cooldown
agent_policy_denied
sensitive_route_unavailable
ready_but_no_eligible_agent
ready_agent_idle_undispatched
readonly_projection_only
review_authority_unavailable
timing_unavailable
dispatch_dependency_missing
```

v1 投影不得输出 `lease_conflict` 或 `lease_claim_timeout`；那些属于后续 lease
authority。未知枚举必须变成 `source_invalid`。

## 6. Freshness

- `fresh`：`status=available`，`observed_at` 与 `freshness_deadline` 都存在，
  且 `evaluated_at <= freshness_deadline`，且 `git_dirty=false`。
- `dirty`：`git_dirty=true`；保留可审计 revision，但不可派并附 `source_dirty`。
- `stale`：曾经 available，但 `evaluated_at > freshness_deadline`；必须附
  `source_stale`。ready / review / available-agent 均不得形成可派工结论。
- `unknown`：时间缺失、倒置、非 UTC，或来源非 available；按来源状态附
  `source_unavailable` / `source_invalid` / `authoritative_plan_ambiguous`。

freshness deadline 由 adapter 按来源 SLA 显式提供；projection 不猜 TTL。
Herdr / agent observation 逐行使用同一规则。过期 observation 不能产生
`available`。remote transport unknown 不得推断 local lost / exited。

## 7. 45 / 90 / 135 时间语义

```text
RECONCILIATION_INTERVAL_SECONDS = 45
READY_WITHOUT_DISPATCH_GRACE_SECONDS = 90
ONSET_TO_OPEN_UPPER_BOUND_SECONDS = 135
```

`next_reconciliation_at = evaluated_at + 45s`。SCHED-002 不创建 loop；事件可
提前触发重新投影，但不推迟既定兜底。真实发生到 open 的最坏上界是
`90s + one tick <= 135s`，不得声称“真实发生后 90 秒内一定告警”。

`ready_agent_idle_undispatched` 只在 eligibility 二分匹配非空且没有对应
active offer 时成立：

```text
fresh authoritative source
AND ready writer count > 0
AND remaining writer capacity > 0
AND eligible_pairs != ∅
AND active dispatch/offer count == 0
AND dispatch.available == false
```

`eligible_pairs` 必须同时通过 work / phase / role / author / sensitivity /
identity / generation / lease / capacity 门。两个独立计数
`ready_count > 0 && idle_count > 0` 不能证明可派 pairing。

计时规则：

- 调用方显式传入可信 `first_observed_at` 时：`t < 90` 为 `pending`，
  `t=89.999` 不 open，`t=90` 才 `open`。
- 无 previous / 无持久 observation：`absent` + `timing_unavailable`，不得凭
  wall clock 立即 open。
- source unavailable / stale 打断连续成立时间，恢复后重新计龄。
- 条件消失输出一次 `resolved`；再次出现重新计龄。
- 进程重启没有 previous 时重新计龄。

ready/capacity 成立但 `eligible_pairs` 为空时，立即输出
`ready_but_no_eligible_agent`，不得误报 idle-undispatched。

## 8. Dispatch 依赖清单

缺任一项，`dispatch.available` 必须为 false，并附
`dispatch_dependency_missing`：

```text
SCHED-001-authority-contract
SCHED-002-readonly-projection
workspace_workitem_review_authority
workspace_agent_identity_store
runtime_attachment_generation_handshake
author_gate_harness_catalog
delivery_expected_revision_cas
dispatch_lease_store
reconciler_intents
harness_durable_claim_heartbeat
api_web_attention_wiring
```

不得从 idle Claude Pane + opaque instance ID 拼装自动派工。

## 9. Logical lease uniqueness 与 fencing

active 唯一约束落在逻辑资源，**不含** source revision：

```text
UNIQUE ACTIVE (project_id, workspace_id, source_kind, source_id, phase)
UNIQUE ACTIVE (agent_instance_id)
```

`source_revision` / digest 是 claim、heartbeat、complete 的 fence，不是放宽
active uniqueness 的维度。首版同一 Agent 跨 phase 单活。R1 active lease 未
清理时，同一逻辑工作的 R2 不得获得第二条 active lease；必须先在同一
scheduler transaction 内 fence/cancel R1。迟到的旧 generation complete 必须
conflict 且零状态变化。

所有未来 lease mutation 必须校验完整元组：

```text
lease_id + fencing_token + expected_lease_revision
+ source_id + source_revision + source_digest
+ agent_instance_id + identity_revision
+ attachment_id + attachment_revision + runtime_generation
+ workspace/node runtime_epoch + policy_revision
```

## 10. CAS saga 与 heartbeat owner

真正 dispatch 不是一次 lease INSERT，而是 Operation / saga：

1. offer 只保留候选，计入 occupancy 并集，不改 Delivery status
2. Agent claim 后仍为 `claimed_pending_source`
3. 只有绑定 exact source revision 的 Lead-authorized Delivery CAS receipt
   成功，才进入 `working` 并交付可执行 work reference
4. Delivery CAS 失败：lease 进入 compensating / terminal；不得静默改 car
   status，也不得立刻向另一 Agent 重发同一逻辑工作
5. 失败 / 重启只从 operation receipts 收敛，不猜测补写另一权威

capacity=1 且 C1 offer 已提交但 Delivery 仍 planned 时，并发 reconcile C2
必须看到 occupancy=1 并拒绝第二个 offer。同一 car 已 active 且同 car lease
active 时 occupancy 只计一次。

heartbeat 所有权：

- offer TTL：等待相同 identity + generation 主动 claim
- claimed heartbeat：必须由当前 generation credential 显式发送
- Pane 观测只更新 runtime observation freshness，**绝不**续工作 lease
- heartbeat expiry：fence lease 并把 Agent 投影为不可派；不自动把 Delivery
  标成 blocked / planned / accepted
- 重新派同一 writer work 前必须完成 source owner handoff / CAS

合同状态反例必须比较 heartbeat 前后 lease 状态：Pane event、不同 lease ID 或旧
generation credential 对 expiry/fence 零变化；只有当前 lease ID + generation
credential 才能更新 expiry。该反例是纯 projection helper，不实现或调用 runtime
lease store。

现有 `coordination.maintain_live_claims` 只能保留消息 receipt 语义，不能复用
为工作 lease。

## 11. Sensitivity 与 verified harness

```text
sensitivity 缺失                 → unclassified，不可领取
sensitive_security + verified claude|kimi → 可通过该门
sensitive_security + 其他/unknown/别名 → agent_policy_denied
无 eligible claude|kimi          → sensitive_route_unavailable
identity 偏好不等于 verified attachment harness → 按实际 verified harness 拒绝
```

canonical 敏感 allowlist 只有 verified `claude` 与 `kimi`。`kimi-code`、
`codex`、`opencode`、`zcode`、`unknown` 一律不可领取敏感工作。policy 必须
引用 versioned Harness Catalog ID 与当前 verified RuntimeAttachment；不能只
看 identity 偏好、Pane label 或 alias。sensitivity 或 policy revision 改变时
fence 旧 lease，不静默降级。

本车只做普通 allowlist 状态投影，不运行敏感、攻击或泄漏测试。

## 12. Review 投影

Delivery `status=review` 可映射为 bootstrap review work，并保留 exact
reviewer / base / fixed SHA；除此之外只可返回 `review_authority_unavailable`。
Pane `done` 或后台 task `done` 不得制造 ReviewPacket，不得把仍为
`in_progress` 的 car 推进到 review。Reviewer 必须与作者 identity 不同，且
该 Agent 不得已有任一 phase 的 active lease。给 review car 派 reviewer 不得
把 writer occupancy 减 1。

合同反例必须同时给出作者、已有 writer/reviewer lease 的候选和可用 reviewer：前二者
没有 pair，后者保留 pair，不能用“pair 数为零”的常量断言代替。linked
Delivery/Assignment 的投影必须返回原 observed status（包含 `in_progress` 与
`closed`）；不得调用 close/write adapter，也不得靠常量字段证明 no-writeback。

## 13. 无自动启动 / 无自动合并

合同反例必须用 fake adapter 断言：projection / 合同校验对
`start_agent`、`pane_send`、`merge`、`accept`、`close_assignment`、
`write_delivery` 的调用次数为 0。不得构造攻击载荷。
