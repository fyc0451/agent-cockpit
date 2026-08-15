import { ApiError, ProtocolError, apiGet, type ApiResult } from './client'
import { apiPost } from './registry'

export const WORKSPACE_EXECUTION_API = {
  members: (projectId: string, workspaceId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/members`,
  preparation: (projectId: string, workspaceId: string, workItemId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/work-items/${encodeURIComponent(workItemId)}/preparation`,
  attach: (projectId: string, workspaceId: string, workItemId: string) =>
    `${WORKSPACE_EXECUTION_API.preparation(projectId, workspaceId, workItemId)}/attach`,
  detach: (projectId: string, workspaceId: string, workItemId: string) =>
    `${WORKSPACE_EXECUTION_API.preparation(projectId, workspaceId, workItemId)}/detach`,
} as const

export interface WorkspaceExecutionMember {
  identity_id: string
  display_name: string
  role: 'member'
  lifecycle: 'active'
  revision: number
}

export interface WorkspaceExecutionPrincipal {
  identity_id: string
  generation: number
}

export interface WorkspaceExecutionCheckout {
  checkout_id: string
  status: string
  source_head: string
  source_tree: string
  ref_kind: string
  revision: number
}

export interface WorkspaceExecutionLease {
  lease_id: string
  status: string
  generation: number
  revision: number
}

export interface WorkspaceExecutionAttachment {
  attachment_id: string
  status: string
  provider: string
  harness: string
  generation: number
  identity_verified: boolean
  revision: number
}

export interface WorkspacePreparation {
  work_item_id: string
  state: string
  revision: number
  work_item_status: 'unassigned'
  identity: WorkspaceExecutionMember
  principal: WorkspaceExecutionPrincipal
  checkout: WorkspaceExecutionCheckout | null
  lease: WorkspaceExecutionLease | null
  attachment: WorkspaceExecutionAttachment | null
}

export interface WorkspaceMemberListData {
  items: WorkspaceExecutionMember[]
  next_cursor: string | null
}

function fail(field: string): never {
  throw new ProtocolError(`workspace execution 响应字段缺失或类型错误：${field}`)
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

function requiredInt(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) fail(field)
  return value
}

export function assertWorkspaceExecutionMember(raw: unknown): WorkspaceExecutionMember {
  const value = object(raw, 'member')
  exactKeys(value, ['identity_id', 'display_name', 'role', 'lifecycle', 'revision'], 'member 键集')
  if (value.role !== 'member') fail('member.role')
  if (value.lifecycle !== 'active') fail('member.lifecycle')
  if (typeof value.display_name !== 'string' || value.display_name === '') fail('member.display_name')
  return {
    identity_id: requiredId(value.identity_id, 'member.identity_id'),
    display_name: value.display_name,
    role: 'member',
    lifecycle: 'active',
    revision: requiredInt(value.revision, 'member.revision'),
  }
}

function assertCheckout(raw: unknown): WorkspaceExecutionCheckout {
  const value = object(raw, 'checkout')
  exactKeys(
    value,
    ['checkout_id', 'status', 'source_head', 'source_tree', 'ref_kind', 'revision'],
    'checkout 键集',
  )
  return {
    checkout_id: requiredId(value.checkout_id, 'checkout.checkout_id'),
    status: requiredId(value.status, 'checkout.status'),
    source_head: requiredId(value.source_head, 'checkout.source_head'),
    source_tree: requiredId(value.source_tree, 'checkout.source_tree'),
    ref_kind: requiredId(value.ref_kind, 'checkout.ref_kind'),
    revision: requiredInt(value.revision, 'checkout.revision'),
  }
}

function assertLease(raw: unknown): WorkspaceExecutionLease {
  const value = object(raw, 'lease')
  exactKeys(value, ['lease_id', 'status', 'generation', 'revision'], 'lease 键集')
  return {
    lease_id: requiredId(value.lease_id, 'lease.lease_id'),
    status: requiredId(value.status, 'lease.status'),
    generation: requiredInt(value.generation, 'lease.generation'),
    revision: requiredInt(value.revision, 'lease.revision'),
  }
}

function assertAttachment(raw: unknown): WorkspaceExecutionAttachment {
  const value = object(raw, 'attachment')
  exactKeys(
    value,
    ['attachment_id', 'status', 'provider', 'harness', 'generation', 'identity_verified', 'revision'],
    'attachment 键集',
  )
  if (typeof value.identity_verified !== 'boolean') fail('attachment.identity_verified')
  return {
    attachment_id: requiredId(value.attachment_id, 'attachment.attachment_id'),
    status: requiredId(value.status, 'attachment.status'),
    provider: requiredId(value.provider, 'attachment.provider'),
    harness: requiredId(value.harness, 'attachment.harness'),
    generation: requiredInt(value.generation, 'attachment.generation'),
    identity_verified: value.identity_verified,
    revision: requiredInt(value.revision, 'attachment.revision'),
  }
}

export function assertWorkspacePreparation(raw: unknown): WorkspacePreparation {
  const value = object(raw, 'preparation')
  exactKeys(
    value,
    [
      'work_item_id',
      'state',
      'revision',
      'work_item_status',
      'identity',
      'principal',
      'checkout',
      'lease',
      'attachment',
    ],
    'preparation 键集',
  )
  if (value.work_item_status !== 'unassigned') fail('preparation.work_item_status')
  if (typeof value.state !== 'string' || value.state === '') fail('preparation.state')
  const identity = assertWorkspaceExecutionMember(value.identity)
  const principal = object(value.principal, 'preparation.principal')
  exactKeys(principal, ['identity_id', 'generation'], 'principal 键集')
  const principalId = requiredId(principal.identity_id, 'principal.identity_id')
  if (principalId !== identity.identity_id) fail('principal.identity_id')
  return {
    work_item_id: requiredId(value.work_item_id, 'preparation.work_item_id'),
    state: value.state,
    revision: requiredInt(value.revision, 'preparation.revision'),
    work_item_status: 'unassigned',
    identity,
    principal: {
      identity_id: principalId,
      generation: requiredInt(principal.generation, 'principal.generation'),
    },
    checkout: value.checkout === null ? null : assertCheckout(value.checkout),
    lease: value.lease === null ? null : assertLease(value.lease),
    attachment: value.attachment === null ? null : assertAttachment(value.attachment),
  }
}

export function assertWorkspaceMemberListData(raw: unknown): WorkspaceMemberListData {
  const data = object(raw, 'members')
  exactKeys(data, ['items', 'next_cursor'], 'members 键集')
  if (!Array.isArray(data.items)) fail('members.items')
  if (data.next_cursor !== null && typeof data.next_cursor !== 'string') fail('members.next_cursor')
  return {
    items: data.items.map(assertWorkspaceExecutionMember),
    next_cursor: data.next_cursor as string | null,
  }
}

export async function listWorkspaceMembers(
  projectId: string,
  workspaceId: string,
): Promise<ApiResult<WorkspaceMemberListData>> {
  try {
    const result = await apiGet<unknown>(WORKSPACE_EXECUTION_API.members(projectId, workspaceId))
    return { ...result, data: assertWorkspaceMemberListData(result.data) }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { data: { items: [], next_cursor: null }, meta: null }
    }
    throw error
  }
}

export async function createWorkspaceMember(
  projectId: string,
  workspaceId: string,
  displayName: string,
  idempotencyKey: string,
): Promise<ApiResult<WorkspaceExecutionMember>> {
  const result = await apiPost<{ display_name: string }, unknown>(
    WORKSPACE_EXECUTION_API.members(projectId, workspaceId),
    { display_name: displayName },
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspaceExecutionMember(result.data) }
}

export async function getWorkspacePreparation(
  projectId: string,
  workspaceId: string,
  workItemId: string,
): Promise<ApiResult<WorkspacePreparation | null>> {
  try {
    const result = await apiGet<unknown>(
      WORKSPACE_EXECUTION_API.preparation(projectId, workspaceId, workItemId),
    )
    return { ...result, data: assertWorkspacePreparation(result.data) }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return { data: null, meta: null }
    }
    throw error
  }
}

export async function createWorkspacePreparation(
  projectId: string,
  workspaceId: string,
  workItemId: string,
  identityId: string,
  idempotencyKey: string,
): Promise<ApiResult<WorkspacePreparation>> {
  const result = await apiPost<{ identity_id: string }, unknown>(
    WORKSPACE_EXECUTION_API.preparation(projectId, workspaceId, workItemId),
    { identity_id: identityId },
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspacePreparation(result.data) }
}

export async function attachWorkspacePreparation(
  projectId: string,
  workspaceId: string,
  workItemId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<ApiResult<WorkspacePreparation>> {
  const result = await apiPost<{ expected_revision: number }, unknown>(
    WORKSPACE_EXECUTION_API.attach(projectId, workspaceId, workItemId),
    { expected_revision: expectedRevision },
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspacePreparation(result.data) }
}

export async function detachWorkspacePreparation(
  projectId: string,
  workspaceId: string,
  workItemId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<ApiResult<WorkspacePreparation>> {
  const result = await apiPost<{ expected_revision: number }, unknown>(
    WORKSPACE_EXECUTION_API.detach(projectId, workspaceId, workItemId),
    { expected_revision: expectedRevision },
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspacePreparation(result.data) }
}
