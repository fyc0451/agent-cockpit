// 团队时间线只走 /api/team/ledger*。不走本机群发送。

import { ApiError } from './client'

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export interface TeamLedgerMessage {
  id: string
  topic: string
  hub: string
  kind: string
  sender: string
  text: string
  to: string[]
  ts: number
  handed_to_leader?: boolean
}

function parseMessage(raw: unknown): TeamLedgerMessage | null {
  if (!isObj(raw)) return null
  if (typeof raw.id !== 'string' || typeof raw.topic !== 'string') return null
  if (typeof raw.text !== 'string' || typeof raw.sender !== 'string') return null
  const to = Array.isArray(raw.to)
    ? raw.to.filter((item): item is string => typeof item === 'string')
    : []
  return {
    id: raw.id,
    topic: raw.topic,
    hub: typeof raw.hub === 'string' ? raw.hub : '',
    kind: typeof raw.kind === 'string' ? raw.kind : 'me',
    sender: raw.sender,
    text: raw.text,
    to,
    ts: typeof raw.ts === 'number' ? raw.ts : 0,
    handed_to_leader: raw.handed_to_leader === true,
  }
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json()
    if (isObj(body) && typeof body.detail === 'string') return body.detail
    if (typeof body === 'string') return body
  } catch {
    /* ignore */
  }
  return fallback
}

export async function listTeamLedger(topic: string): Promise<TeamLedgerMessage[]> {
  const response = await fetch(
    `/api/team/ledger/messages?topic=${encodeURIComponent(topic)}`,
    { credentials: 'include' },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'team_ledger_list_failed',
      message: await readError(response, '读取团队时间线失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = isObj(data) && Array.isArray(data.messages) ? data.messages : []
  return rows.map(parseMessage).filter((row): row is TeamLedgerMessage => row !== null)
}

export async function sendTeamLedger(
  topic: string,
  text: string,
): Promise<TeamLedgerMessage> {
  const response = await fetch('/api/team/ledger/messages', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, text, kind: 'me', sender: 'human' }),
  })
  if (!response.ok) {
    throw new ApiError({
      code: 'team_ledger_send_failed',
      message: await readError(response, '发送团队消息失败'),
      retryable: false,
      status: response.status,
    })
  }
  const data = await response.json()
  const row = parseMessage(isObj(data) ? data.message : null)
  if (!row) {
    throw new ApiError({
      code: 'protocol_error',
      message: '团队发送响应格式错误',
      retryable: false,
    })
  }
  return row
}
