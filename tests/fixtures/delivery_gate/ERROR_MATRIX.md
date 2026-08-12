# Delivery Gate 错误矩阵

本文档列出了所有无效 fixture 及其对应的机器拦截错误类型和稳定错误 code。

## 有效 Fixture

| 文件 | 说明 |
|------|------|
| `valid_minimal.json` | 最小有效交付单，符合所有校验规则 |

## 无效 Fixture 错误矩阵

### 4.1 结构校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_01_unknown_field.json` | 未知字段 | `unknown_field` |
| `invalid_02_duplicate_car_id.json` | 重复 car ID | `duplicate_car_id` |
| `invalid_03_unknown_dependency.json` | 未知依赖 | `unknown_dependency` |
| `invalid_04_dag_cycle.json` | DAG 成环 | `dependency_cycle` |
| `invalid_18_duplicate_json_key.json` | JSON 重复 key | `duplicate_json_key` |
| `invalid_19_wrong_types.json` | 类型错误（布尔冒充整数） | `wrong_types` |

### 4.2 必填字段校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_05_missing_scope.json` | 缺少 scope | `missing_field` |
| `invalid_06_missing_acceptance.json` | 缺少 acceptance | `missing_field` |
| `invalid_07_missing_rollback.json` | 缺少 rollback | `missing_field` |
| `invalid_11_missing_production_impact.json` | 缺少 production_impact | `missing_field` |

### 4.3 人员校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_08_in_progress_without_owner.json` | in_progress 无 owner | `owner_required` |
| `invalid_09_review_without_reviewer.json` | review 无独立 reviewer | `independent_reviewer_required` |

> **Opaque Instance ID 格式**: Cockpit 格式为 `i-` 开头 + 26 位小写 base32（`[a-z2-7]`）
> 正则: `^i-[a-z2-7]{26}$`
> ❌ **invalid_instance_id**: 格式不符合要求

### 4.4 SHA 校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_10_review_without_sha.json` | review 无 SHA | `exact_sha_required` |
| `invalid_12_fixed_sha_not_exist.json` | fixed_sha 不存在 | `fixed_sha_not_found` |

### 4.5 Scope 校验

> 注意：此类别需要实际 git diff，fixture 仅作结构示例，实际校验在 F1 实现时完成
>
> 稳定错误 code: `invalid_scope` (scope 不是有效的路径前缀) / `scope_violation` (diff 越出声明的 scope)

### 4.6 依赖校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_13_accepted_with_blocked_dep.json` | 未完成依赖却标记 accepted | `dependency_not_satisfied` |

### 4.7 WIP 校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_14_exceed_wip_limit.json` | WIP 超过限制 | `writer_wip_exceeded` |

### 4.8 发布时长校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_15_release_timeout.json` | 发布超时 | `release_timeout` |

### 4.9 跨模块 BLOCK 校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_16_second_block_continue.json` | 第二次 BLOCK 后继续 | `reslice_required` |

### 4.10 用户验收校验

| 文件 | 错误类型 | 稳定错误 code |
|------|----------|----------------|
| `invalid_17_agent_marks_user_accepted.json` | 缺少 user_acceptance_evidence | `user_acceptance_evidence_required` |
| `invalid_20_forged_user_evidence.json` | JSON 伪造用户验收证据 | `forged_user_evidence` |

> **Foundation v1 用户验收**: 没有可信用户证据源，任何仅凭 JSON 的 `user_accepted` 都 fail-closed

## 覆盖统计

- **有效 fixtures**: 1
- **无效 fixtures**: 20
- **覆盖反例类别**: 13/13 ✅

## 稳定错误 code 列表

| Code | 说明 |
|------|------|
| `unknown_field` | 未知字段 |
| `duplicate_car_id` | 重复 car ID |
| `unknown_dependency` | 未知依赖 |
| `dependency_cycle` | DAG 成环 |
| `duplicate_json_key` | JSON 任意层存在重复 key |
| `wrong_types` | 字段类型不匹配（如布尔值冒充整数） |
| `missing_field` | 缺少必填字段 |
| `invalid_scope` | scope 不是有效的路径前缀 |
| `invalid_instance_id` | instance ID 格式不符合 `i-[a-z2-7]{26}` |
| `owner_required` | in_progress 无 owner |
| `independent_reviewer_required` | review 无独立 reviewer |
| `exact_sha_required` | review/accepted 缺少 exact SHA |
| `fixed_sha_not_found` | fixed_sha 不存在于仓库 |
| `scope_violation` | diff 越出声明的 scope |
| `dependency_not_satisfied` | 依赖未完成 |
| `writer_wip_exceeded` | WIP 超过限制 |
| `release_timeout` | 发布超时 |
| `reslice_required` | 第二次跨模块 BLOCK 后仍继续 |
| `user_acceptance_evidence_required` | user_accepted 缺少验收证据 |
| `forged_user_evidence` | Foundation v1 仅凭 JSON 的 user_accepted |

## 使用方式

F1 实现 `delivery_gate.py` 时，使用以下命令验证所有反例：

```bash
for f in tests/fixtures/delivery_gate/invalid_*.json; do
  echo "Testing $f..."
  python3 scripts/delivery_gate.py check "$f" && echo "❌ 应该拒绝但通过了" || echo "✅ 正确拒绝"
done

python3 scripts/delivery_gate.py check tests/fixtures/delivery_gate/valid_minimal.json || echo "❌ 有效单被拒绝"
```
