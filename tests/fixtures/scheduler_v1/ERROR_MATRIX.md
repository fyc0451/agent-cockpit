# Scheduler v1 错误与反例矩阵

普通合同/fixture 验证。不包含安全攻击、路径穿越、fuzz 或泄漏载荷。

## 钉死金样

| 文件 | 说明 | 稳定结论 |
| --- | --- | --- |
| `pinned_a75e8c4.json` | writer base `a75e8c4` + plan digest `b555dd4f…` | capacity `3/1/2`，ready=`PROJ-004`,`SCHED-001`，dispatch=false |
| `pinned_parent_0bbbc56.json` | 父提交 plan digest `50102e76…` | 不同 fingerprint；capacity `3/2/1`，ready 仅 `SCHED-001` |

## 结构 fail-closed → `source_invalid`

| 文件 | 原因 |
| --- | --- |
| `invalid_unknown_field.json` | 未知顶层字段 |
| `invalid_unknown_reason.json` | 未知 reason code |
| `invalid_duplicate_work_id.json` | 重复 `source_id` |
| `invalid_sha256.json` | `plan_sha256` 非 `sha256:<64hex>` |
| `invalid_timestamp_zulu.json` | 时间戳使用 `Z` 而非 `+00:00` |
| `invalid_active_dispatch_nonzero.json` | `active_dispatch_count != 0` |
| `invalid_active_dispatch_negative.json` | 负数 dispatch count |
| `invalid_unknown_sensitivity.json` | 非冻结 sensitivity 枚举 |
| `invalid_kimi_code_as_verified.json` | 把观察别名 `kimi-code` 标成 verified harness |

## 来源四态

| 文件 | 稳定 reason |
| --- | --- |
| `source_unavailable.json` | `source_unavailable` |
| `source_invalid.json` | `source_invalid` |
| `source_ambiguous.json` | `authoritative_plan_ambiguous` |
| `source_stale.json` | `source_stale` |

## P0/P1 状态机反例

| 文件 | 冻结规则 |
| --- | --- |
| `scenario_unlinked_authorities.json` | 无 typed link 不得合并；`unlinked_authorities` |
| `scenario_linked_no_sync.json` | 有 link 也不得把 Assignment close |
| `scenario_pane_idle_no_identity.json` | Pane 非身份；`no_stable_identity` / `runtime_generation_unavailable` |
| `scenario_transport_unknown.json` | remote unknown ≠ local lost |
| `scenario_sensitivity_missing.json` | 缺字段=`unclassified`，idle Claude 也不可领 |
| `scenario_sensitivity_opencode.json` | verified OpenCode → `agent_policy_denied` |
| `scenario_sensitivity_kimi_code_alias.json` | `kimi-code` 别名不可领取敏感工作 |
| `scenario_sensitivity_attachment_mismatch.json` | 按 verified attachment 拒绝 |
| `scenario_no_eligible_pair.json` | ready+idle 无边 → `ready_but_no_eligible_agent` |
| `scenario_review_occupies_wip.json` | review 仍占 writer WIP |
| `scenario_pane_done_no_review.json` | Pane done 不制造 ReviewPacket |
| `scenario_lease_r1_blocks_r2.json` | 逻辑唯一键不含 revision |
| `scenario_occupancy_union.json` | offer 占用并集，拒绝第二 offer |
| `scenario_occupancy_no_double_count.json` | 同 car + lease 只计一次 |
| `scenario_heartbeat_pane_ignored.json` | Pane 事件不续 dispatch heartbeat |
| `scenario_author_cannot_review.json` | 作者不得审自己 |
| `scenario_wip2_ungated.json` | gate 未 accepted 时 effective=2 |
