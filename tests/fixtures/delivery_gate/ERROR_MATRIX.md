# Delivery Gate 错误矩阵

本文档列出了所有无效 fixture 及其对应的机器拦截错误类型。

## 有效 Fixture

| 文件 | 说明 |
|------|------|
| `valid_minimal.json` | 最小有效交付单，符合所有校验规则 |

## 无效 Fixture 错误矩阵

### 4.1 结构校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_01_unknown_field.json` | 未知字段 | `unknown_field_should_be_rejected` |
| `invalid_02_duplicate_car_id.json` | 重复 car ID | `duplicate-car` 出现两次 |
| `invalid_03_unknown_dependency.json` | 未知依赖 | 依赖 `non-existent-car-id` 不存在 |
| `invalid_04_dag_cycle.json` | DAG 成环 | `car-a` 依赖 `car-b`，`car-b` 依赖 `car-a` |

### 4.2 必填字段校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_05_missing_scope.json` | 缺少 scope | `car-05` 缺少 `scope` 字段 |
| `invalid_06_missing_acceptance.json` | 缺少 acceptance | `car-06` 缺少 `acceptance` 字段 |
| `invalid_07_missing_rollback.json` | 缺少 rollback | `car-07` 缺少 `rollback` 字段 |
| `invalid_11_missing_production_impact.json` | 缺少 production_impact | `car-11` 缺少 `production_impact` 字段 |

### 4.3 人员校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_08_in_progress_without_owner.json` | in_progress 无 owner | `car-08` 状态为 `in_progress` 但 `owner_instance_id` 为 null |
| `invalid_09_review_without_reviewer.json` | review 无独立 reviewer | `car-09` 状态为 `review` 但 `reviewer_instance_id` 与 `owner_instance_id` 相同 |

### 4.4 SHA 校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_10_review_without_sha.json` | review 无 SHA | `car-10` 状态为 `review` 但 `base_sha`/`fixed_sha` 为 null |
| `invalid_12_fixed_sha_not_exist.json` | fixed_sha 不存在 | `ffffffffffffffffffffffffffffffffffffffff` 不在仓库中 |

### 4.5 Scope 校验

> 注意：此类别需要实际 git diff，fixture 仅作结构示例，实际校验在 F1 实现时完成

### 4.6 依赖校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_13_accepted_with_blocked_dep.json` | 未完成依赖却标记 accepted | `car-13` 标记为 `accepted` 但依赖 `car-dependency` 仍为 `in_progress` |

### 4.7 WIP 校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_14_exceed_wip_limit.json` | WIP 超过限制 | 3 个车处于 `in_progress` 状态，超过 `writer_wip=2` 限制 |

### 4.8 发布时长校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_15_release_timeout.json` | 发布超时 | `release_started_at` 为 `2026-08-12T20:00:00Z`，已超过 15 分钟上限 |

### 4.9 跨模块 BLOCK 校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_16_second_block_continue.json` | 第二次 BLOCK 后继续 | `cross_module_block_count=2` 但状态仍为 `review` |

### 4.10 用户验收校验

| 文件 | 错误类型 | 预期错误信息 |
|------|----------|--------------|
| `invalid_17_agent_marks_user_accepted.json` | agent 直接标记 user_accepted | `user_acceptance_required=true` 但状态为 `user_accepted`（只能由用户设置） |

## 覆盖统计

- **有效 fixtures**: 1
- **无效 fixtures**: 17
- **覆盖反例类别**: 10/10 ✅

## 使用方式

F1 实现 `delivery_gate.py` 时，使用以下命令验证所有反例：

```bash
for f in tests/fixtures/delivery_gate/invalid_*.json; do
  echo "Testing $f..."
  python3 scripts/delivery_gate.py check "$f" && echo "❌ 应该拒绝但通过了" || echo "✅ 正确拒绝"
done

python3 scripts/delivery_gate.py check tests/fixtures/delivery_gate/valid_minimal.json || echo "❌ 有效单被拒绝"
```
