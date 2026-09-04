// 团队时间线只走远端 Team Hub 代理，不进入本机群聊账本。

import { ApiError } from './client'

function isObj(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export interface TeamMessage {
  id: number
  subject: string
  body_md: string
  mention_handles: string[]
  importance: string
  created_ts: string
  sender_name: string
  sender_human_id: number | null
  sender_kind: string
  sender_agent: string | null
  attachments: TeamAttachment[]
  replyEvidence: {
    contextAvailable: boolean
    contextFingerprint: string | null
    sha: string | null
    dirty: boolean | null
    handoffUpdated: string | null
    consulted: boolean
    answerSource: 'team_agent' | 'context_pack' | 'local_lead'
    createdTs: number
  } | null
}

export interface TeamAttachment {
  id: string
  filename: string
  media_type: string
  size: number
  sha256: string
}

export interface TeamMessagePage {
  messages: TeamMessage[]
  hasMore: boolean
  nextBeforeId: number | null
}

function parseReplyEvidence(raw: unknown): TeamMessage['replyEvidence'] {
  if (!isObj(raw)) return null
  if (
    typeof raw.context_available !== 'boolean'
    || typeof raw.consulted !== 'boolean'
    || !['team_agent', 'context_pack', 'local_lead'].includes(String(raw.answer_source))
    || typeof raw.created_ts !== 'number'
    || !Number.isFinite(raw.created_ts)
  ) return null
  const fingerprint = raw.context_fingerprint
  const sha = raw.sha
  const dirty = raw.dirty
  const handoffUpdated = raw.handoff_updated
  if (fingerprint !== null && (
    typeof fingerprint !== 'string' || !/^[0-9a-f]{64}$/.test(fingerprint)
  )) return null
  if (sha !== null && (typeof sha !== 'string' || !/^[0-9a-f]{40,64}$/.test(sha))) {
    return null
  }
  if (dirty !== null && typeof dirty !== 'boolean') return null
  if (handoffUpdated !== null && (
    typeof handoffUpdated !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:T\S{1,64})?$/.test(handoffUpdated)
  )) return null
  return {
    contextAvailable: raw.context_available,
    contextFingerprint: fingerprint,
    sha,
    dirty,
    handoffUpdated,
    consulted: raw.consulted,
    answerSource: raw.answer_source as 'team_agent' | 'context_pack' | 'local_lead',
    createdTs: raw.created_ts,
  }
}

function parseMessage(raw: unknown): TeamMessage | null {
  if (!isObj(raw) || typeof raw.id !== 'number') return null
  if (typeof raw.body_md !== 'string' || typeof raw.sender_name !== 'string') return null
  return {
    id: raw.id,
    subject: typeof raw.subject === 'string' ? raw.subject : '',
    body_md: raw.body_md,
    mention_handles: Array.isArray(raw.mention_handles)
      ? raw.mention_handles.filter((item): item is string => typeof item === 'string')
      : [],
    importance: typeof raw.importance === 'string' ? raw.importance : 'normal',
    created_ts: typeof raw.created_ts === 'string' ? raw.created_ts : '',
    sender_name: raw.sender_name,
    sender_human_id: typeof raw.sender_human_id === 'number' ? raw.sender_human_id : null,
    sender_kind: typeof raw.sender_kind === 'string' ? raw.sender_kind : 'agent',
    sender_agent: typeof raw.sender_agent === 'string' ? raw.sender_agent : null,
    attachments: Array.isArray(raw.attachments)
      ? raw.attachments.map(parseAttachment).filter(
        (item): item is TeamAttachment => item !== null,
      )
      : [],
    replyEvidence: parseReplyEvidence(raw.reply_evidence),
  }
}

function parseAttachment(raw: unknown): TeamAttachment | null {
  if (!isObj(raw)) return null
  if (
    typeof raw.id !== 'string'
    || !/^[0-9a-f]{32}$/.test(raw.id)
    || typeof raw.filename !== 'string'
    || !raw.filename
    || typeof raw.media_type !== 'string'
    || typeof raw.size !== 'number'
    || !Number.isFinite(raw.size)
    || raw.size < 1
    || typeof raw.sha256 !== 'string'
    || !/^[0-9a-f]{64}$/.test(raw.sha256)
  ) return null
  return {
    id: raw.id,
    filename: raw.filename,
    media_type: raw.media_type,
    size: raw.size,
    sha256: raw.sha256,
  }
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json()
    if (isObj(body) && typeof body.detail === 'string') return body.detail
    if (typeof body === 'string') return body
  } catch {
    // 响应不是 JSON 时使用稳定的用户提示。
  }
  return fallback
}

export async function listTeamMessages(
  topic: string,
  options?: { beforeId?: number; limit?: number },
): Promise<TeamMessagePage> {
  const params = new URLSearchParams()
  if (options) {
    const limit = options.limit ?? 80
    params.set('limit', String(limit))
    if (options.beforeId !== undefined) params.set('before_id', String(options.beforeId))
  }
  const query = params.size > 0 ? `?${params.toString()}` : ''
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(topic)}/chat/messages${query}`,
    { credentials: 'include' },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_chat_list_failed',
      message: await readError(response, '读取团队群聊失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = isObj(data) && Array.isArray(data.messages) ? data.messages : []
  const messages = rows.map(parseMessage).filter((row): row is TeamMessage => row !== null)
  const hasMore = isObj(data) && data.has_more === true
  const upstreamCursor = isObj(data) ? data.next_before_id : null
  const nextBeforeId = typeof upstreamCursor === 'number' && upstreamCursor > 0
    ? upstreamCursor
    : (hasMore && messages.length > 0 ? messages[0].id : null)
  return { messages, hasMore, nextBeforeId }
}

export async function sendTeamMessage(
  topic: string,
  text: string,
  mentionHandles: string[] | null,
  attachmentIds: string[] = [],
): Promise<void> {
  const payload: Record<string, unknown> = {
    subject: '群聊消息',
    body_md: text,
    importance: 'normal',
  }
  if (mentionHandles !== null) payload.mention_handles = mentionHandles
  if (attachmentIds.length > 0) payload.attachment_ids = attachmentIds
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(topic)}/support-requests`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_chat_send_failed',
      message: await readError(response, '发送团队消息失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

export async function uploadTeamAttachment(
  topic: string,
  file: File,
): Promise<TeamAttachment> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(
    `/api/team-auth/projects/${encodeURIComponent(topic)}/attachments`,
    { method: 'POST', credentials: 'include', body: form },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_attachment_upload_failed',
      message: await readError(response, '上传团队附件失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const attachment = parseAttachment(await response.json())
  if (!attachment) {
    throw new ApiError({
      code: 'team_attachment_protocol_error',
      message: '团队附件响应格式错误',
      retryable: false,
    })
  }
  return attachment
}

export async function deleteTeamAttachment(topic: string, attachmentId: string): Promise<void> {
  const response = await fetch(
    `/api/team-auth/projects/${encodeURIComponent(topic)}/attachments/${encodeURIComponent(attachmentId)}`,
    { method: 'DELETE', credentials: 'include' },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_attachment_delete_failed',
      message: await readError(response, '删除团队附件失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

export function teamAttachmentDownloadUrl(topic: string, attachmentId: string): string {
  return `/api/team-auth/projects/${encodeURIComponent(topic)}/attachments/${encodeURIComponent(attachmentId)}`
}

export async function handoffTeamMessageToLocal(
  topic: string,
  payload: {
    requestId: string
    messageId: number
    targetSession: string
    scope: string
    acceptance: string
  },
): Promise<{ targetSession: string; lead: string; idempotent: boolean; notified: boolean }> {
  const response = await fetch(
    `/api/team-auth/projects/${encodeURIComponent(topic)}/local-handoffs`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: payload.requestId,
        message_id: payload.messageId,
        target_session: payload.targetSession,
        scope: payload.scope,
        acceptance: payload.acceptance,
      }),
    },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_local_handoff_failed',
      message: await readError(response, '交给本地会话失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const raw = await response.json()
  if (!isObj(raw) || typeof raw.target_session !== 'string' || typeof raw.lead !== 'string') {
    throw new ApiError({
      code: 'team_local_handoff_protocol_error',
      message: '本地会话接收响应格式错误',
      retryable: false,
    })
  }
  return {
    targetSession: raw.target_session,
    lead: raw.lead,
    idempotent: raw.idempotent === true,
    notified: raw.notified === true,
  }
}
