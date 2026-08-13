# Agent Cockpit 交付框架合同 (Delivery Contract)

> **版本**: 2（兼容 schema v1）
> **项目**: agent-cockpit
> **生效日期**: 2026-08-12
> **Foundation SHA**: 待定 (F3 完成后记录)

本文档定义 Agent Cockpit 交付框架的机器可校验合同规范。所有发布车（Release Car）必须遵守此合同才能通过 `delivery_gate.py` 的校验。

## 一、交付单结构

### 1.1 根级别必填字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | integer | ✅ | `1` 或 `2`；v1 保持原行为，v2 启用门控扩容 |
| `goal_id` | string | ✅ | 稳定机器 ID（如 `foundation-f0`） |
| `user_journey` | string | ✅ | 一句话用户旅程 |
| `non_goals` | string[] | ✅ | 明确不做项 |
| `baseline` | object | ✅ | 开工基线（见下表） |
| `limits` | object | ✅ | 发布限制（见下表） |
| `cars` | object[] | ✅ | 发布车数组 |

### 1.2 Baseline 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `main_sha` | string | ✅ | 开工时 main 分支 exact 40 位 SHA |
| `production_version` | string | ✅ | 生产版本号（如 `0.3.3`） |
| `production_source_sha` | string | ✅ | 生产 source exact 40 位 SHA |

### 1.3 Limits 字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `writer_wip` | integer | ✅ | 2 | 同时处于 in_progress/review 的 writer 最大数 |
| `writer_wip_gates` | object[] | v2 必填 | - | 依次声明已规划的 `n -> n+1` writer 容量 gate |
| `release_minutes` | integer | ✅ | 15 | 单车发布时间上限（分钟） |
| `cross_module_blocks_before_reslice` | integer | ✅ | 2 | 跨模块 BLOCK 后必须停止的阈值 |

`writer_wip=2` 是无扩容证据时的 fail-closed 基线，不是永久人数上限。schema v2
通过 `writer_wip_gates` 形成连续容量链：

```json
{
  "writer_wip": 2,
  "writer_wip_gates": [
    {"car_id": "DELIVERY-002-wip3-gate", "from": 2, "to": 3},
    {"car_id": "DELIVERY-003-wip4-gate", "from": 3, "to": 4}
  ]
}
```

每个 gate 必须引用同名 `DELIVERY-NNN-wipM-gate` car，且只允许连续增加 1；后续 gate car
必须依赖前一个 gate car。只有该 car 进入 `accepted` 或 `user_accepted` 后，对应容量才生效。
`planned`、`in_progress`、`review`、`blocked`、`cancelled` 均保留上一个已验收容量。

## 二、Release Car 必填字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 稳定机器 ID（如 `foundation-f0`） |
| `title` | string | ✅ | 人类可读标题 |
| `status` | enum | ✅ | 状态（见下表） |
| `depends_on` | string[] | ✅ | 依赖的车 ID 数组，无依赖为 `[]` |
| `scope` | string[] | ✅ | 允许改动的路径前缀（相对 repo 根的精确文件或目录，非 glob） |
| `acceptance` | object[] | ✅ | 验收证据数组，每项为 `{"command": string, "passed": boolean}` |
| `rollback` | string | ✅ | 可执行或明确的回退方式 |
| `production_impact` | enum | ✅ | 生产影响级别（见下表） |
| `owner_instance_id` | string\|null | ✅ | opaque instance ID，未领取时为 `null` |
| `reviewer_instance_id` | string\|null | ✅ | 独立 reviewer opaque ID，review 时必填 |
| `base_sha` | string\|null | ✅ | 进入 review 前的 exact 40 位 SHA |
| `fixed_sha` | string\|null | ✅ | 审查通过后的 exact 40 位 SHA |
| `cross_module_block_count` | integer | ✅ | 跨模块 BLOCK 次数计数 |
| `release_started_at` | string\|null | ✅ | ISO 8601 时间戳，releasing 时必填 |
| `user_acceptance_required` | boolean | ✅ | 是否需要用户验收 |
| `user_acceptance_evidence` | string\|null | ✅ | 用户验收证据（user_acceptance_required 且状态为 user_accepted 时必填） |

### 2.1 Status 枚举

| 状态 | 说明 | 进入条件 |
|------|------|----------|
| `planned` | 已规划，未开始 | 初始状态 |
| `in_progress` | 进行中 | 有 owner_instance_id |
| `review` | 审查中 | 有 owner + reviewer + base_sha + fixed_sha |
| `accepted` | 已接受 | 依赖已接受，验收通过 |
| `releasing` | 发布中 | accepted + 有回滚点 + release_started_at |
| `canary` | 金丝雀中 | releasing 验收通过 |
| `user_accepted` | 用户已验收 | 用户明确验收（仅当 user_acceptance_required=true） |
| `blocked` | 已阻塞 | 依赖或校验失败 |
| `cancelled` | 已取消 | 明确终止态 |

### 2.2 Production Impact 枚举

| 值 | 说明 |
|----|------|
| `none` | 无生产影响（如测试、文档） |
| `dark` | 暗发布（未启用） |
| `canary` | 金丝雀发布 |
| `release` | 正式发布 |

## 三、状态转换规则

```
planned -> in_progress -> review -> accepted -> releasing -> canary -> user_accepted
              |             |          |            |
              +----------> blocked <---+------------+
```

### 3.1 进入 in_progress
- 必须有 `owner_instance_id`（opaque ID）

### 3.2 进入 review
- 必须有 `owner_instance_id`
- 必须有独立 `reviewer_instance_id`（不能与 owner 相同）
- 必须有 exact `base_sha`
- 必须有 exact `fixed_sha` 且存在于仓库

### 3.3 进入 accepted
- 所有依赖（`depends_on`）必须处于 `accepted` 或 `user_accepted` 状态
- 所有 `acceptance` 证据的 `passed` 为 `true`（CLI 只校验证据，不执行命令）
- 候选 diff 不超出声明的 `scope`

### 3.4 进入 releasing
- 必须处于 `accepted` 状态
- 必须有明确的 `rollback`
- 必须有 `release_started_at`（ISO 8601）
- 检查发布时长不超过 `release_minutes` 上限

### 3.5 进入 user_accepted
- 仅当 `user_acceptance_required=true` 时
- 必须由用户明确验收，agent 不能直接标记

### 3.6 进入 blocked
- 依赖车处于 `blocked` 或 `cancelled`
- 校验失败（如 DAG 成环、diff 越界）
- `cross_module_block_count >= cross_module_blocks_before_reslice`

## 四、机器拦截规则（Must-Reject）

`delivery_gate.py check` 必须以稳定 error code 拒绝以下情况：

### 4.1 结构校验
- ❌ **unknown_field**: 未知字段
- ❌ **duplicate_car_id**: 重复 car ID
- ❌ **unknown_dependency**: 未知依赖（依赖的车不存在）
- ❌ **dependency_cycle**: DAG 成环（直接或间接依赖循环）
- ❌ **duplicate_json_key**: JSON 任意层存在重复 key
- 字段类型不匹配使用对应字段的稳定错误 code（如布尔值冒充整数返回 `invalid_block_count`）

### 4.2 必填字段校验
- ❌ **missing_field**: 缺少必填字段（scope、acceptance、rollback、production_impact、user_acceptance_evidence）
- ❌ **invalid_scope**: scope 不是有效的路径前缀

### 4.3 人员校验
- ❌ **owner_required**: `in_progress` 状态但无 `owner_instance_id`（非 null）
- ❌ **independent_reviewer_required**: `review` 状态但无独立 `reviewer_instance_id`（与 owner 相同或为 null）

### 4.4 SHA 校验
- ❌ **exact_sha_required**: `review` 或 `accepted` 状态但无 exact base/fixed SHA
- ❌ **fixed_sha_not_found**: `fixed_sha` 不存在于仓库中

### 4.5 Scope 校验
- ❌ **scope_violation**: 候选 diff（base_sha 到 fixed_sha）越出声明的 `scope`
- ❌ **scope_ownership_overlap**: 可能并行或同时 active 的 car 声明了相交的路径 ownership

scope 是 repo-relative 精确文件或目录前缀，不是 glob。两个 scope 的路径组件完全相同，或其中
一个是另一个的祖先目录，即视为相交。同一 car 内重复、等价或祖先/后代 scope 也属于
`invalid_scope`。有 DAG 先后关系且未同时 active 的 car 可以串行复用同一 scope；两个同时
active 的 car 即使声明了依赖也不得重叠。

### 4.6 依赖校验
- ❌ **dependency_not_satisfied**: 依赖车未完成（非 `accepted`/`user_accepted`）却标记当前车为 `accepted`/`releasing`

### 4.7 WIP 校验
- ❌ **writer_wip_exceeded**: 同时处于 `in_progress` 或 `review` 状态的 writer 超过 `writer_wip` 限制
- ❌ **writer_wip_gate_required**: schema v2 的 gate 缺失、跳级、命名不匹配、重复或未形成依赖链

`check` 和 `ready` 必须共享同一个有效容量计算。评审、分析、测试以及其他不拥有写入 car 的
工作不计入 writer WIP；限制对象是 active writer car，不是已启动的 agent 数量。

### 4.8 发布时长校验
- ❌ **release_timeout**: `releasing` 状态超过 `release_minutes` 上限（硬错）
- ❌ **release_start_in_future**: `release_started_at` 晚于当前 UTC 时间

### 4.9 跨模块 BLOCK 校验
- ❌ **reslice_required**: `cross_module_block_count >= 2` 时仍继续 `review` 或 `releasing`

### 4.10 用户验收校验
- ❌ **user_acceptance_evidence_required**: `user_acceptance_required=true` 且状态为 `user_accepted` 但缺少 `user_acceptance_evidence`
- ❌ **user_acceptance_evidence_required**: Foundation v1 没有可信用户证据源，任何仅凭 JSON 的 `user_accepted` 都 fail-closed

**重要**: Foundation v1 阶段，CLI 不执行 acceptance 命令，只校验已记录的证据。`user_acceptance_evidence` 必须来自外部可信源（不在 JSON 中），仅凭 JSON 字段的 user_accepted 状态将被拒绝。

## 五、命令输出格式

### 5.1 check 命令
```bash
python3 scripts/delivery_gate.py check .delivery/cockpit-next.json
```
- 成功：退出码 0，输出 "Contract valid"
- 失败：退出码非 0，输出具体错误

### 5.2 ready 命令
```bash
python3 scripts/delivery_gate.py ready .delivery/cockpit-next.json --json
```
输出可独立领取的发布车（JSON）：
```json
{
  "ready": ["car-id-1", "car-id-2"],
  "waiting_on": {
    "car-id-3": ["依赖的车-id"],
    "car-id-4": ["阻塞原因"]
  }
}
```

### 5.3 release-check 命令
```bash
python3 scripts/delivery_gate.py release-check .delivery/cockpit-next.json <car-id>
```
校验特定车的发布条件：
- fixed SHA 存在
- 所有 Gate 通过
- 回滚点可用
- 发布时长未超限

## 六、Opaque Instance ID

### 6.1 格式要求
- **Cockpit opaque instance 格式**: `i-` 开头，后跟 26 位小写 base32 字符
- 正则表达式: `^i-[a-z2-7]{26}$`
- 示例: `i-2k24eo3a5i4hgmdawy5xuyrsba`

### 6.2 字段使用
- `owner_instance_id`: 领取车后必填，格式必须符合 Cockpit opaque instance
- `reviewer_instance_id`: review 状态必填，格式必须符合 Cockpit opaque instance
- 不得使用 display name、花名或可重复的临时 ID
- 通过 `am-register` 获取并写入注册文件

### 6.3 校验
- ❌ **invalid_instance_id**: instance ID 格式不符合 `i-[a-z2-7]{26}`

## 七、Foundation MVP 发布车

| 车 | ID | 内容 | 验收 |
|----|----|----|----|
| F0 | `foundation-f0` | 冻结 contract 与反例 | 合同文档、有效/无效 fixture、错误矩阵 |
| F1 | `foundation-f1` | 实现严格只读 CLI | `check/ready/release-check --json` 稳定输出 |
| F2 | `foundation-f2` | 用独立重构项目 dogfood | 只有 N0 ready，R0-R6 列出 exact waiting_on |
| F3 | `foundation-f3` | 独立复审、重启演练与冻结 | fixed SHA、审查证据、foundation tag |

## 八、停止条件

触发任一条件立即停止当车，不继续扩张：
- 需要新表、新服务、新 API 或前端页面
- 需要修改现有 assignment 语义
- 生产代码超过约 300 行
- Foundation 总时间超过 6 小时
- 候选连续两轮出现跨模块 BLOCK
- 必须触碰升级引擎、主题、terminal replay 或生产配置
- 无法在不启动生产发布的情况下验收
