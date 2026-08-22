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

export async function listTeamMessages(topic: string): Promise<TeamMessage[]> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(topic)}/chat/messages`,
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
  return rows.map(parseMessage).filter((row): row is TeamMessage => row !== null)
}

export async function sendTeamMessage(topic: string, text: string): Promise<void> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(topic)}/support-requests`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: '群聊消息', body_md: text, importance: 'normal' }),
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
