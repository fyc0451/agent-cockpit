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
      },
    ]
  })
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

export async function sendSessionMail(
  session: string,
  text: string,
  to: string[],
  options?: { ledgerOnly?: boolean },
): Promise<{ mail_error: string | null }> {
  const raw = await legacyPost(
    `/api/chat/sessions/${encodeURIComponent(session)}/mail`,
    { text, to, ledger_only: options?.ledgerOnly === true },
  )
  if (!isObj(raw)) return { mail_error: null }
  return {
    mail_error: typeof raw.mail_error === 'string' && raw.mail_error ? raw.mail_error : null,
  }
}

/** 终端提交进瀑布流：只写账本，不转发 Hub、不叫醒 pane。 */
export async function recordTerminalLine(session: string, text: string): Promise<void> {
  const body = text.trim()
  if (!body) return
  await sendSessionMail(session, body, ['终端'], { ledgerOnly: true })
}

export interface SessionMailMessage {
  id: string
  sender: string
  program: string
  text: string
  to: string[]
  thread: string
  ts: number
}

export function mailBelongsToSession(thread: string, session: string): boolean {
  return Boolean(session && thread && thread === session)
}

export async function fetchSessionMail(session: string): Promise<SessionMailMessage[]> {
  const raw = await legacyGet(`/api/chat/sessions/${encodeURIComponent(session)}/mail`)
  if (!isObj(raw)) fail('mail')
  const rows = Array.isArray(raw.messages) ? raw.messages : []
  return rows.flatMap((row) => {
    if (!isObj(row)) return []
    const id = typeof row.id === 'string' ? row.id : typeof row.id === 'number' ? String(row.id) : ''
    if (!id) return []
    const to = Array.isArray(row.to)
      ? row.to.filter((item): item is string => typeof item === 'string' && item !== '')
      : []
    const thread = typeof row.thread === 'string' ? row.thread : ''
    if (!mailBelongsToSession(thread, session)) return []
    return [
      {
        id,
        sender: typeof row.sender === 'string' ? row.sender : '',
        program: typeof row.program === 'string' ? row.program : '',
        text: typeof row.text === 'string' ? row.text : '',
        to,
        thread,
        ts: typeof row.ts === 'number' && Number.isFinite(row.ts) ? row.ts : 0,
      },
    ]
  })
}
