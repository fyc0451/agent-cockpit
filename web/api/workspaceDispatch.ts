import { ProtocolError, apiGet, type ApiResult } from './client'
import { apiPost } from './registry'

export const WORKSPACE_DISPATCH_API = {
  detail: (projectId: string, workspaceId: string, workItemId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/work-items/${encodeURIComponent(workItemId)}`,
  dispatch: (projectId: string, workspaceId: string, workItemId: string) =>
    `${WORKSPACE_DISPATCH_API.detail(projectId, workspaceId, workItemId)}/dispatch`,
} as const

export const WORKSPACE_DISPATCH_ERROR_CODES = [
  'invalid_argument',
  'idempotency_key_required',
  'project_not_found',
  'workspace_not_found',
  'work_item_not_found',
  'preparation_not_found',
  'workspace_not_active',
  'idempotency_conflict',
  'stale_revision',
  'stale_generation',
  'delivery_conflict',
  'claim_conflict',
  'claim_not_active',
  'execution_terminal',
  'runtime_capability_invalid',
  'runtime_unavailable',
  'operation_journal_unavailable',
  'wakeup_outcome_unknown',
  'schema_missing',
  'workspace_work_schema_missing',
  'migration_required',
  'future_schema',
  'schema_fingerprint_mismatch',
  'store_unsafe',
  'store_corrupt',
  'store_read_failed',
  'store_write_failed',
] as const

export interface WorkspaceDispatchRequest {
  expected_work_revision: number
  expected_preparation_revision: number
}

export interface WorkspaceDispatchResult {
  operation_id: string
  outcome: 'succeeded'
}

function fail(field: string): never {
  throw new ProtocolError(`workspace dispatch 响应字段缺失或类型错误：${field}`)
}

function object(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail(field)
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[], field: string): void {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail(field)
  }
}

function requiredId(value: unknown, field: string): string {
  if (typeof value !== 'string' || value === '') fail(field)
  return value
}

function requiredRevision(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) fail(field)
  return value
}

export function assertWorkspaceDispatchResult(raw: unknown): WorkspaceDispatchResult {
  const value = object(raw, 'dispatch')
  exactKeys(value, ['operation_id', 'outcome'], 'dispatch 键集')
  if (value.outcome !== 'succeeded') fail('dispatch.outcome')
  return {
    operation_id: requiredId(value.operation_id, 'dispatch.operation_id'),
    outcome: 'succeeded',
  }
}

export async function getWorkspaceDispatchWorkRevision(
  projectId: string,
  workspaceId: string,
  workItemId: string,
): Promise<number> {
  const result = await apiGet<unknown>(
    WORKSPACE_DISPATCH_API.detail(projectId, workspaceId, workItemId),
  )
  const detail = object(result.data, 'detail')
  exactKeys(detail, ['thread', 'work_item', 'claim', 'receipts'], 'detail 键集')
  const workItem = object(detail.work_item, 'detail.work_item')
  exactKeys(
    workItem,
    [
      'work_item_id',
      'source_message_id',
      'status',
      'acceptance',
      'constraints',
      'revision',
      'updated_at',
    ],
    'detail.work_item 键集',
  )
  if (requiredId(workItem.work_item_id, 'detail.work_item.work_item_id') !== workItemId) {
    fail('detail.work_item.work_item_id')
  }
  if (workItem.status !== 'unassigned') fail('detail.work_item.status')
  return requiredRevision(workItem.revision, 'detail.work_item.revision')
}

export async function dispatchWorkspaceWork(
  projectId: string,
  workspaceId: string,
  workItemId: string,
  request: WorkspaceDispatchRequest,
  idempotencyKey: string,
): Promise<ApiResult<WorkspaceDispatchResult>> {
  const result = await apiPost<WorkspaceDispatchRequest, unknown>(
    WORKSPACE_DISPATCH_API.dispatch(projectId, workspaceId, workItemId),
    request,
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspaceDispatchResult(result.data) }
}
