// 群聊：会话目录内的文件 + Agent Mail 发送。

import { legacyGet } from './localSlice'
import { legacyPost } from './legacyHerdr'
import { ApiError } from './client'
import type { DirList, FileRead, SearchResult } from './legacyFiles'

function fail(field: string): never {
  throw new ApiError({
    code: 'protocol_error',
    message: `chat session 响应必填字段缺失或类型错误：${field}`,
    retryable: false,
  })
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export interface ChatSkill {
  id: string
  label: string
  insert: string
}

export interface AgentMailStatus {
  connected: boolean
  pane_id: string
  details: {
    has_mail_name: boolean
    has_config_path: boolean
    has_agent_session: boolean
    can_send_mail: boolean
  }
  error?: string
}

export async function fetchAgentMailStatus(
  session: string,
  paneId: string,
): Promise<AgentMailStatus> {
  const raw = await legacyGet(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}/mail-status`,
  )
  if (!isObj(raw)) fail('mail-status')
  return {
    connected: typeof raw.connected === 'boolean' ? raw.connected : false,
    pane_id: typeof raw.pane_id === 'string' ? raw.pane_id : paneId,
    details: isObj(raw.details)
      ? {
          has_mail_name: typeof raw.details.has_mail_name === 'boolean' ? raw.details.has_mail_name : false,
          has_config_path: typeof raw.details.has_config_path === 'boolean' ? raw.details.has_config_path : false,
          has_agent_session: typeof raw.details.has_agent_session === 'boolean' ? raw.details.has_agent_session : false,
          can_send_mail: typeof raw.details.can_send_mail === 'boolean' ? raw.details.can_send_mail : false,
        }
      : {
          has_mail_name: false,
          has_config_path: false,
          has_agent_session: false,
          can_send_mail: false,
        },
    error: typeof raw.error === 'string' ? raw.error : undefined,
  }
}


export async function fetchChatSkills(): Promise<ChatSkill[]> {
  const raw = await legacyGet('/api/chat/skills')
  if (!isObj(raw) || !Array.isArray(raw.skills)) return []
  return raw.skills.flatMap((row) => {
    if (!isObj(row)) return []
    const id = typeof row.id === 'string' ? row.id : ''
    const label = typeof row.label === 'string' ? row.label : id
    const insert = typeof row.insert === 'string' ? row.insert : ''
    return id && insert ? [{ id, label, insert }] : []
  })
}

export async function fetchSessionDirList(session: string, path: string): Promise<DirList> {
  const raw = await legacyGet(
    `/api/chat/sessions/${encodeURIComponent(session)}/files?path=${encodeURIComponent(path)}`,
  )
  if (!isObj(raw)) fail('list')
  const entries = Array.isArray(raw.entries) ? raw.entries : []
  return {
    path: typeof raw.path === 'string' ? raw.path : path,
    type: typeof raw.type === 'string' ? raw.type : null,
    entries: entries.map((e, i) => {
      if (!isObj(e)) fail(`entries[${i}]`)
      return {
        name: typeof e.name === 'string' ? e.name : '',
        type: typeof e.type === 'string' ? e.type : 'file',
        size: typeof e.size === 'number' ? e.size : 0,
        ext: typeof e.ext === 'string' ? e.ext : '',
      }
    }),
  }
}

export async function fetchSessionFileContent(session: string, path: string): Promise<FileRead> {
  const raw = await legacyGet(
    `/api/chat/sessions/${encodeURIComponent(session)}/files/read?path=${encodeURIComponent(path)}`,
  )
  if (!isObj(raw)) fail('read')
  return {
    path: typeof raw.path === 'string' ? raw.path : path,
    text: typeof raw.text === 'string' ? raw.text : '',
    binary: raw.binary === true,
    size: typeof raw.size === 'number' ? raw.size : 0,
  }
}

export interface SessionGitSummary {
  repo: boolean
  branch: string
  branches: string[]
  files: number
  stat: string
  diff?: string
}

export async function fetchSessionGit(
  session: string,
  opts?: { diff?: boolean },
): Promise<SessionGitSummary> {
  const query = opts?.diff ? '?diff=1' : ''
  const raw = await legacyGet(`/api/chat/sessions/${encodeURIComponent(session)}/git${query}`)
  if (!isObj(raw)) fail('git')
  const branches = Array.isArray(raw.branches)
    ? raw.branches.filter((item): item is string => typeof item === 'string' && item !== '')
    : []
  return {
    repo: raw.repo === true,
    branch: typeof raw.branch === 'string' ? raw.branch : '',
    branches,
    files: typeof raw.files === 'number' && Number.isFinite(raw.files) ? raw.files : 0,
    stat: typeof raw.stat === 'string' ? raw.stat : '',
    diff: typeof raw.diff === 'string' ? raw.diff : undefined,
  }
}

export async function searchSessionFiles(session: string, q: string): Promise<SearchResult[]> {
  const raw = await legacyGet(
    `/api/chat/sessions/${encodeURIComponent(session)}/files/search?q=${encodeURIComponent(q)}`,
  )
  if (!isObj(raw)) fail('search')
  const rows = Array.isArray(raw.results) ? raw.results : Array.isArray(raw.matches) ? raw.matches : []
  return rows.flatMap((r) => {
    if (!isObj(r)) return []
    const p = typeof r.path === 'string' ? r.path : ''
    if (!p) return []
    return [
      {
        path: p,
        name: typeof r.name === 'string' ? r.name : p.split('/').pop() || p,
        type: typeof r.type === 'string' ? r.type : 'file',
        relative: typeof r.relative === 'string' && r.relative ? r.relative : undefined,
      },
    ]
  })
}

export function sessionFileDownloadUrl(session: string, path: string): string {
  return `/api/chat/sessions/${encodeURIComponent(session)}/files/download?path=${encodeURIComponent(path)}`
}

export function sessionFileRawUrl(session: string, path: string): string {
  return `/api/chat/sessions/${encodeURIComponent(session)}/files/raw?path=${encodeURIComponent(path)}`
}

export async function uploadChatFile(
  session: string,
  file: File,
): Promise<{ path: string; absolutePath: string; filename: string; size: number }> {
  const body = new FormData()
  body.append('file', file, file.name || 'screenshot.png')
  let res: Response
  try {
    res = await fetch(`/api/chat/sessions/${encodeURIComponent(session)}/files/upload`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      body,
    })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }
  let parsed: unknown = null
  try {
    parsed = await res.json()
  } catch {
    parsed = null
  }
  if (!res.ok) {
    const detail = isObj(parsed) && typeof parsed.detail === 'string' ? parsed.detail : null
    throw new ApiError({
      code: res.status >= 500 ? 'server_error' : 'http_error',
      message: detail ?? `上传失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }
  if (!isObj(parsed) || typeof parsed.path !== 'string' || !parsed.path) {
    fail('upload.path')
  }
  const rel = typeof parsed.rel === 'string' ? parsed.rel : ''
  return {
    path: rel || parsed.path,
    absolutePath: parsed.path,
    filename: typeof parsed.filename === 'string' && parsed.filename ? parsed.filename : file.name || '附件',
    size: typeof parsed.size === 'number' && Number.isFinite(parsed.size) ? parsed.size : file.size,
  }
}

export type ChatDelivery = 'interrupt' | 'queue'

export async function sendSessionMail(
  session: string,
  text: string,
  to: string[],
  options?: { ledgerOnly?: boolean; delivery?: ChatDelivery; direct?: boolean; source?: string },
): Promise<{ mail_error: string | null }> {
  const raw = await legacyPost(
    `/api/chat/sessions/${encodeURIComponent(session)}/mail`,
    {
      text,
      to,
      ledger_only: options?.ledgerOnly === true,
      delivery: options?.delivery === 'interrupt' ? 'interrupt' : 'queue',
      ...(options?.direct === true ? { direct: true } : {}),
      ...(options?.source ? { source: options.source } : {}),
    },
  )
  if (!isObj(raw)) return { mail_error: null }
  return {
    mail_error: typeof raw.mail_error === 'string' && raw.mail_error ? raw.mail_error : null,
  }
}

/** Boss 换群 Leader：后端改记录、账本记事件、叫醒全员宣告。 */
export async function setSessionLeader(session: string, mailName: string): Promise<unknown> {
  return legacyPost(
    `/api/chat/sessions/${encodeURIComponent(session)}/leader`,
    { mail_name: mailName },
  )
}

/** 群成员 agent pane 上的终端输入进瀑布流；空 to 不写。只写账本，不转发 Hub。 */
export async function recordTerminalLine(
  session: string,
  text: string,
  to: string,
): Promise<void> {
  const body = text.trim()
  const dest = to.trim()
  if (!body || !dest || dest === '终端') return
  await sendSessionMail(session, body, [dest], { ledgerOnly: true })
}

export interface SessionMailMessage {
  id: string
  sender: string
  program: string
  text: string
  to: string[]
  thread: string
  ts: number
  delivery?: ChatDelivery
  notified_to?: string[]
  read_by?: string[]
  duration_ms?: number
  git?: { files: number; stat: string }
  source?: string
  direct?: boolean
}

export function mailBelongsToSession(thread: string, session: string): boolean {
  return Boolean(session && thread && thread === session)
}

export function sessionMailStreamUrl(session: string): string {
  return `/api/chat/sessions/${encodeURIComponent(session)}/mail/stream`
}

export function parseSessionMailRow(
  row: unknown,
  session: string,
  opts?: { allowMissingThread?: boolean },
): SessionMailMessage | null {
  if (!isObj(row)) return null
  const id = typeof row.id === 'string' ? row.id : typeof row.id === 'number' ? String(row.id) : ''
  if (!id) return null
  const to = Array.isArray(row.to)
    ? row.to.filter((item): item is string => typeof item === 'string' && item !== '')
    : []
  const rawThread = typeof row.thread === 'string' ? row.thread : ''
  const thread = rawThread || (opts?.allowMissingThread ? session : '')
  if (!mailBelongsToSession(thread, session)) return null
  const names = (value: unknown): string[] | undefined => {
    if (!Array.isArray(value)) return undefined
    const items = value.filter((item): item is string => typeof item === 'string' && item !== '')
    return items.length > 0 ? items : undefined
  }
  const duration = row.duration_ms
  const notified = names(row.notified_to)
  const read = names(row.read_by)
  const message: SessionMailMessage = {
    id,
    sender: typeof row.sender === 'string' ? row.sender : '',
    program: typeof row.program === 'string' ? row.program : '',
    text: typeof row.text === 'string' ? row.text : '',
    to,
    thread,
    ts: typeof row.ts === 'number' && Number.isFinite(row.ts) ? row.ts : 0,
  }
  if (row.delivery === 'queue' || row.delivery === 'interrupt') {
    message.delivery = row.delivery
  }
  if (notified) message.notified_to = notified
  if (read) message.read_by = read
  if (typeof duration === 'number' && Number.isFinite(duration) && duration >= 0) {
    message.duration_ms = duration
  }
  if (typeof row.source === 'string' && row.source.trim()) {
    message.source = row.source.trim()
  }
  if (row.direct === true) message.direct = true
  const git = row.git
  if (
    isObj(git)
    && typeof git.files === 'number' && Number.isFinite(git.files) && git.files >= 0
    && typeof git.stat === 'string'
  ) {
    message.git = { files: git.files, stat: git.stat }
  }
  return message
}

export function applyMailStreamEvent(
  current: SessionMailMessage[] | undefined,
  event: string,
  data: string,
  session: string,
): SessionMailMessage[] {
  const prev = current ?? []
  let payload: unknown
  try {
    payload = JSON.parse(data)
  } catch {
    return prev
  }
  if (event === 'snapshot') {
    const rows = isObj(payload) && Array.isArray(payload.messages) ? payload.messages : []
    const incoming = rows.flatMap((row) => {
      const parsed = parseSessionMailRow(row, session)
      return parsed ? [parsed] : []
    })
    return preferLedgerMail(prev, incoming)
  }
  if (event === 'replace' || event === 'receipt') {
    if (!isObj(payload) || typeof payload.id !== 'string' || !payload.id) return prev
    return prev.map((item) => {
      if (item.id !== payload.id) return item
      if (event === 'replace') {
        return typeof payload.text === 'string' ? { ...item, text: payload.text } : item
      }
      const names = (value: unknown): string[] | undefined => {
        if (!Array.isArray(value)) return undefined
        const items = value.filter((item): item is string => typeof item === 'string' && item !== '')
        return items.length > 0 ? items : undefined
      }
      return {
        ...item,
        notified_to: names(payload.notified_to) ?? item.notified_to,
        read_by: names(payload.read_by) ?? item.read_by,
      }
    })
  }
  const row = parseSessionMailRow(payload, session, { allowMissingThread: true })
  if (!row) return prev
  if (event === 'message') {
    if (prev.some((item) => item.id === row.id)) {
      return prev.map((item) => (item.id === row.id ? { ...item, ...row } : item))
    }
    return [...prev, row].sort((a, b) => (a.ts !== b.ts ? a.ts - b.ts : a.id.localeCompare(b.id)))
  }
  return prev
}

/** 第二次 /mail 或缺 thread 的 Hub 信不得把账本里已有的 msg_* 擦掉。 */
export function preferLedgerMail(
  current: SessionMailMessage[] | undefined,
  incoming: SessionMailMessage[],
): SessionMailMessage[] {
  if (!current?.length) return incoming
  const incomingIds = new Set(incoming.map((row) => row.id))
  const kept = current.filter((row) => row.id.startsWith('msg_') && !incomingIds.has(row.id))
  if (kept.length === 0) return incoming
  return [...incoming, ...kept].sort((a, b) => {
    if (a.ts !== b.ts) return a.ts - b.ts
    return a.id.localeCompare(b.id)
  })
}

export async function fetchSessionMail(
  session: string,
  source: 'ledger' | 'all' = 'all',
): Promise<SessionMailMessage[]> {
  const query = source === 'ledger' ? '?source=ledger' : ''
  const raw = await legacyGet(`/api/chat/sessions/${encodeURIComponent(session)}/mail${query}`)
  if (!isObj(raw)) fail('mail')
  const rows = Array.isArray(raw.messages) ? raw.messages : []
  return rows.flatMap((row) => {
    const parsed = parseSessionMailRow(row, session)
    return parsed ? [parsed] : []
  })
}
