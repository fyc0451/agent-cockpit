import { ProtocolError, apiGet, type ApiResult } from './client'
import { apiPost } from './registry'

export const WORKSPACE_WORK_API = {
  items: (projectId: string, workspaceId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/work-items`,
} as const

export interface CreateWorkspaceWorkRequest {
  body: string
  acceptance: string | null
  constraints: string | null
}

export interface WorkspaceWorkAggregate {
  thread: Record<string, unknown> & {
    thread_id: string
  }
  root_message: Record<string, unknown> & {
    message_id: string
    thread_id: string
    author_kind: 'boss'
    author_ref: null
    body: string
  }
  work_item: Record<string, unknown> & {
    source_message_id: string
    acceptance: string | null
    constraints: string | null
    status: 'unassigned'
  }
}

export interface WorkspaceWorkListData {
  items: WorkspaceWorkAggregate[]
  next_cursor: string | null
}

function fail(field: string): never {
  throw new ProtocolError(`workspace work 响应字段缺失或类型错误：${field}`)
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

function nullableString(value: unknown, field: string): string | null {
  if (value === null) return null
  if (typeof value !== 'string') fail(field)
  return value
}

function requiredId(value: unknown, field: string): string {
  if (typeof value !== 'string' || value === '') fail(field)
  return value
}

export function assertWorkspaceWorkAggregate(raw: unknown): WorkspaceWorkAggregate {
  const aggregate = object(raw, 'item')
  exactKeys(aggregate, ['thread', 'root_message', 'work_item'], 'item 键集')

  const thread = object(aggregate.thread, 'item.thread')
  const rootMessage = object(aggregate.root_message, 'item.root_message')
  const workItem = object(aggregate.work_item, 'item.work_item')

  const threadId = requiredId(thread.thread_id, 'item.thread.thread_id')
  const messageId = requiredId(rootMessage.message_id, 'item.root_message.message_id')
  const rootThreadId = requiredId(rootMessage.thread_id, 'item.root_message.thread_id')
  if (rootMessage.author_kind !== 'boss') fail('item.root_message.author_kind')
  if (rootMessage.author_ref !== null) fail('item.root_message.author_ref')
  if (typeof rootMessage.body !== 'string') fail('item.root_message.body')
  if (rootThreadId !== threadId) fail('item.root_message.thread_id')

  const sourceMessageId = requiredId(workItem.source_message_id, 'item.work_item.source_message_id')
  if (sourceMessageId !== messageId) fail('item.work_item.source_message_id')
  if (workItem.status !== 'unassigned') fail('item.work_item.status')

  return {
    thread: { ...thread, thread_id: threadId },
    root_message: {
      ...rootMessage,
      message_id: messageId,
      thread_id: rootThreadId,
      author_kind: 'boss',
      author_ref: null,
      body: rootMessage.body,
    },
    work_item: {
      ...workItem,
      source_message_id: sourceMessageId,
      acceptance: nullableString(workItem.acceptance, 'item.work_item.acceptance'),
      constraints: nullableString(workItem.constraints, 'item.work_item.constraints'),
      status: 'unassigned',
    },
  }
}

export function assertWorkspaceWorkListData(raw: unknown): WorkspaceWorkListData {
  const data = object(raw, 'work-items')
  exactKeys(data, ['items', 'next_cursor'], 'work-items 键集')
  if (!Array.isArray(data.items)) fail('work-items.items')
  if (data.next_cursor !== null && typeof data.next_cursor !== 'string') {
    fail('work-items.next_cursor')
  }
  return {
    items: data.items.map(assertWorkspaceWorkAggregate),
    next_cursor: data.next_cursor as string | null,
  }
}

export async function listWorkspaceWork(
  projectId: string,
  workspaceId: string,
): Promise<ApiResult<WorkspaceWorkListData>> {
  const result = await apiGet<unknown>(WORKSPACE_WORK_API.items(projectId, workspaceId))
  return { ...result, data: assertWorkspaceWorkListData(result.data) }
}

export async function createWorkspaceWork(
  projectId: string,
  workspaceId: string,
  request: CreateWorkspaceWorkRequest,
  idempotencyKey: string,
): Promise<ApiResult<WorkspaceWorkAggregate>> {
  const result = await apiPost<CreateWorkspaceWorkRequest, unknown>(
    WORKSPACE_WORK_API.items(projectId, workspaceId),
    request,
    { idempotencyKey },
  )
  return { ...result, data: assertWorkspaceWorkAggregate(result.data) }
}
