import { ProtocolError, apiGet, type ApiResult } from './client'

/** D-W1：GET /work-items/{id} 执行时间线唯一数据源。
 *
 * 解析严格 fail-closed（照 workspaceWork.ts 的 exactKeys 模式）：任何
 * 未知键、错误枚举、形状漂移都抛 ProtocolError，绝不猜测渲染。
 * 禁止渲染/保留：transcript、Pane、绝对路径、fence、token。
 */
export const WORKSPACE_WORK_DETAIL_API = {
  detail: (projectId: string, workspaceId: string, workItemId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}` +
    `/work-items/${encodeURIComponent(workItemId)}`,
} as const

export type WorkStatus = 'unassigned' | 'working' | 'completed' | 'failed'
export type ClaimState = 'pending_gate' | 'active' | 'closed'
export type ReceiptKind = 'delivery' | 'claim' | 'reply' | 'complete' | 'failure'
export type DeliveryOutcome = 'intent' | 'succeeded' | 'outcome_unknown'
export type ReceiptOutcome = DeliveryOutcome | 'ok' | 'failed'

export interface WorkThreadMessage {
  message_id: string
  thread_id: string
  ordinal: number
  message_kind: 'root' | 'reply'
  author_kind: 'boss' | 'agent'
  author_ref: string | null
  author_generation: number | null
  reply_to_message_id: string | null
  body: string
  created_at: string
}

export interface WorkReceipt {
  receipt_id: string
  kind: ReceiptKind
  outcome: ReceiptOutcome
  reason: string | null
  evidence_digest: string | null
  created_at: string
  claim_id: string | null
  message_id: string | null
  identity_id: string | null
  generation: number | null
}

export interface WorkItemClaim {
  claim_id: string
  work_item_id: string
  identity_id: string
  generation: number
  state: ClaimState
  revision: number
}

export interface WorkspaceWorkDetail {
  thread: {
    thread_id: string
    project_id: string
    workspace_id: string
    revision: number
    created_at: string
    messages: WorkThreadMessage[]
  }
  work_item: {
    work_item_id: string
    source_message_id: string
    status: WorkStatus
    acceptance: string | null
    constraints: string | null
    revision: number
    updated_at: string
  }
  claim: WorkItemClaim | null
  receipts: WorkReceipt[]
}

export interface WorkspaceWorkDetailScope {
  projectId: string
  workspaceId: string
  workItemId: string
}

const THREAD_KEYS = [
  'thread_id', 'project_id', 'workspace_id', 'revision', 'created_at', 'messages',
] as const
const MESSAGE_KEYS = [
  'message_id', 'thread_id', 'ordinal', 'message_kind', 'author_kind',
  'author_ref', 'author_generation', 'reply_to_message_id', 'body', 'created_at',
] as const
const WORK_ITEM_KEYS = [
  'work_item_id', 'source_message_id', 'status', 'acceptance', 'constraints',
  'revision', 'updated_at',
] as const
const CLAIM_KEYS = [
  'claim_id', 'work_item_id', 'identity_id', 'generation', 'state', 'revision',
] as const
const RECEIPT_KEYS = [
  'receipt_id', 'kind', 'outcome', 'reason', 'evidence_digest', 'created_at',
  'claim_id', 'message_id', 'identity_id', 'generation',
] as const

const STATUSES: readonly WorkStatus[] = ['unassigned', 'working', 'completed', 'failed']
const CLAIM_STATES: readonly ClaimState[] = ['pending_gate', 'active', 'closed']
const RECEIPT_KINDS: readonly ReceiptKind[] = [
  'delivery', 'claim', 'reply', 'complete', 'failure',
]
const DELIVERY_OUTCOMES: readonly DeliveryOutcome[] = [
  'intent', 'succeeded', 'outcome_unknown',
]

function fail(field: string): never {
  throw new ProtocolError(`work item detail 响应字段缺失或类型错误：${field}`)
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

function string(value: unknown, field: string): string {
  if (typeof value !== 'string' || value === '') fail(field)
  return value
}

function nullableString(value: unknown, field: string): string | null {
  if (value === null) return null
  if (typeof value !== 'string') fail(field)
  return value
}

function nullableId(value: unknown, field: string): string | null {
  if (value === null) return null
  if (typeof value !== 'string' || value === '') fail(field)
  return value
}

function positiveInt(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) fail(field)
  return value
}

function nullablePositiveInt(value: unknown, field: string): number | null {
  if (value === null) return null
  return positiveInt(value, field)
}

function enumValue<T extends string>(
  value: unknown, allowed: readonly T[], field: string,
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) fail(field)
  return value as T
}

function parseMessage(raw: unknown): WorkThreadMessage {
  const message = object(raw, 'detail.thread.messages[]')
  exactKeys(message, MESSAGE_KEYS, 'detail.thread.messages[] 键集')
  return {
    message_id: string(message.message_id, 'message_id'),
    thread_id: string(message.thread_id, 'thread_id'),
    ordinal: positiveInt(message.ordinal, 'ordinal'),
    message_kind: enumValue(message.message_kind, ['root', 'reply'] as const, 'message_kind'),
    author_kind: enumValue(message.author_kind, ['boss', 'agent'] as const, 'author_kind'),
    author_ref: nullableId(message.author_ref, 'author_ref'),
    author_generation: nullablePositiveInt(message.author_generation, 'author_generation'),
    reply_to_message_id: nullableId(message.reply_to_message_id, 'reply_to_message_id'),
    body: typeof message.body === 'string' ? message.body : fail('body'),
    created_at: string(message.created_at, 'created_at'),
  }
}

function parseReceipt(raw: unknown): WorkReceipt {
  const receipt = object(raw, 'detail.receipts[]')
  exactKeys(receipt, RECEIPT_KEYS, 'detail.receipts[] 键集')
  const kind = enumValue(receipt.kind, RECEIPT_KINDS, 'receipts[].kind')
  let outcome: ReceiptOutcome
  if (kind === 'delivery') {
    outcome = enumValue(receipt.outcome, DELIVERY_OUTCOMES, 'receipts[].outcome(delivery)')
  } else if (kind === 'failure') {
    outcome = enumValue(receipt.outcome, ['failed'] as const, 'receipts[].outcome(failure)')
  } else {
    outcome = enumValue(receipt.outcome, ['ok'] as const, `receipts[].outcome(${kind})`)
  }
  return {
    receipt_id: string(receipt.receipt_id, 'receipt_id'),
    kind,
    outcome,
    reason: nullableString(receipt.reason, 'reason'),
    evidence_digest: nullableString(receipt.evidence_digest, 'evidence_digest'),
    created_at: string(receipt.created_at, 'created_at'),
    claim_id: nullableId(receipt.claim_id, 'claim_id'),
    message_id: nullableId(receipt.message_id, 'message_id'),
    identity_id: nullableId(receipt.identity_id, 'identity_id'),
    generation: nullablePositiveInt(receipt.generation, 'generation'),
  }
}

function parseClaim(raw: unknown): WorkItemClaim | null {
  if (raw === null) return null
  const claim = object(raw, 'detail.claim')
  exactKeys(claim, CLAIM_KEYS, 'detail.claim 键集')
  return {
    claim_id: string(claim.claim_id, 'claim_id'),
    work_item_id: string(claim.work_item_id, 'claim.work_item_id'),
    identity_id: string(claim.identity_id, 'identity_id'),
    generation: positiveInt(claim.generation, 'claim.generation'),
    state: enumValue(claim.state, CLAIM_STATES, 'claim.state'),
    revision: positiveInt(claim.revision, 'claim.revision'),
  }
}

export function assertWorkspaceWorkDetail(
  raw: unknown,
  scope: WorkspaceWorkDetailScope,
): WorkspaceWorkDetail {
  const detail = object(raw, 'detail')
  exactKeys(detail, ['thread', 'work_item', 'claim', 'receipts'], 'detail 键集')

  const thread = object(detail.thread, 'detail.thread')
  exactKeys(thread, THREAD_KEYS, 'detail.thread 键集')
  if (!Array.isArray(thread.messages)) fail('detail.thread.messages')

  const workItem = object(detail.work_item, 'detail.work_item')
  exactKeys(workItem, WORK_ITEM_KEYS, 'detail.work_item 键集')

  if (!Array.isArray(detail.receipts)) fail('detail.receipts')

  const threadId = string(thread.thread_id, 'thread_id')
  const projectId = string(thread.project_id, 'project_id')
  const workspaceId = string(thread.workspace_id, 'workspace_id')
  const workItemId = string(workItem.work_item_id, 'work_item_id')
  const sourceMessageId = string(workItem.source_message_id, 'source_message_id')
  const messages = thread.messages
    .slice()
    .sort((a, b) => {
      const left = object(a, 'messages[]') as unknown as { ordinal?: unknown }
      const right = object(b, 'messages[]') as unknown as { ordinal?: unknown }
      return positiveInt(left.ordinal, 'ordinal') - positiveInt(right.ordinal, 'ordinal')
    })
    .map(parseMessage)
  const roots = messages.filter((message) => message.message_kind === 'root')
  if (roots.length !== 1 || messages[0] !== roots[0]) {
    fail('detail.thread.messages(root)')
  }
  const root = roots[0]
  if (
    root.thread_id !== threadId
    || root.message_id !== sourceMessageId
    || root.author_kind !== 'boss'
    || root.author_ref !== null
    || root.author_generation !== null
    || root.reply_to_message_id !== null
  ) {
    fail('detail.thread.messages(root association)')
  }
  if (messages.some((message) => message.thread_id !== threadId)) {
    fail('detail.thread.messages(thread_id)')
  }
  const agentReplies = messages.filter((message) => message.message_kind === 'reply')
  if (agentReplies.some((message) => (
    message.author_kind !== 'agent'
    || message.author_ref === null
    || message.author_generation === null
    || message.reply_to_message_id !== root.message_id
  ))) {
    fail('detail.thread.messages(reply association)')
  }
  if (
    projectId !== scope.projectId
    || workspaceId !== scope.workspaceId
    || workItemId !== scope.workItemId
  ) {
    fail('detail request scope')
  }

  const claim = parseClaim(detail.claim)
  if (claim !== null && claim.work_item_id !== workItemId) {
    fail('detail.claim.work_item_id')
  }
  const receipts = (detail.receipts as unknown[]).map(parseReceipt)
  const status = enumValue(workItem.status, STATUSES, 'work_item.status')
  if (agentReplies.some((message) => (
    claim === null
    || message.author_ref !== claim.identity_id
    || message.author_generation !== claim.generation
  ))) {
    fail('detail.thread.messages(reply principal)')
  }
  for (const receipt of receipts) {
    if (receipt.kind === 'delivery') {
      if (
        receipt.claim_id !== null
        || receipt.message_id !== null
        || receipt.identity_id !== null
        || receipt.generation !== null
      ) {
        fail('detail.receipts(delivery association)')
      }
      continue
    }
    if (
      claim === null
      || receipt.claim_id !== claim.claim_id
      || receipt.identity_id !== claim.identity_id
      || receipt.generation !== claim.generation
    ) {
      fail(`detail.receipts(${receipt.kind} principal)`)
    }
    if (receipt.kind === 'claim') {
      if (receipt.message_id !== null) fail('detail.receipts(claim message)')
      continue
    }
    if (receipt.kind === 'reply' || receipt.kind === 'complete') {
      const reply = agentReplies.find(
        (message) => message.message_id === receipt.message_id,
      )
      if (!reply) fail(`detail.receipts(${receipt.kind} message)`)
    } else if (
      receipt.message_id !== null
      && !agentReplies.some((message) => message.message_id === receipt.message_id)
    ) {
      fail('detail.receipts(failure message)')
    }
  }
  if (status === 'working' && claim?.state !== 'active') {
    fail('detail working claim')
  }
  if (claim?.state === 'active' && status !== 'working') {
    fail('detail active claim status')
  }
  if (status === 'completed') {
    if (claim?.state !== 'closed') fail('detail completed claim')
    const completedReply = agentReplies.find((message) => (
      receipts.some((receipt) => receipt.kind === 'reply' && receipt.message_id === message.message_id)
      && receipts.some((receipt) => (
        receipt.kind === 'complete' && receipt.message_id === message.message_id
      ))
    ))
    if (!completedReply) fail('detail completed receipts')
  }
  if (status === 'failed' && !receipts.some((receipt) => receipt.kind === 'failure')) {
    fail('detail failed receipt')
  }

  return {
    thread: {
      thread_id: threadId,
      project_id: projectId,
      workspace_id: workspaceId,
      revision: positiveInt(thread.revision, 'thread.revision'),
      created_at: string(thread.created_at, 'thread.created_at'),
      messages,
    },
    work_item: {
      work_item_id: workItemId,
      source_message_id: sourceMessageId,
      status,
      acceptance: nullableString(workItem.acceptance, 'acceptance'),
      constraints: nullableString(workItem.constraints, 'constraints'),
      revision: positiveInt(workItem.revision, 'work_item.revision'),
      updated_at: string(workItem.updated_at, 'work_item.updated_at'),
    },
    claim,
    receipts,
  }
}

export async function getWorkspaceWorkDetail(
  projectId: string, workspaceId: string, workItemId: string,
): Promise<ApiResult<WorkspaceWorkDetail>> {
  const result = await apiGet<unknown>(
    WORKSPACE_WORK_DETAIL_API.detail(projectId, workspaceId, workItemId),
  )
  return {
    ...result,
    data: assertWorkspaceWorkDetail(result.data, { projectId, workspaceId, workItemId }),
  }
}
