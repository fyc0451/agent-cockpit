// Cockpit 3.0 工作区 / 群聊账本客户端。侧栏只读这里，不再读 file-roots。

import { legacyGet } from './localSlice'
import { legacyDelete, legacyPost } from './legacyHerdr'
import { ApiError } from './client'

export interface ChatWorkspace {
  id: string
  path: string
  title: string
  created_at: string
  order: number
}

export interface ChatThread {
  id: string
  workspace_id: string
  herdr_session: string
  title: string
  created_at: string
}

export interface ChatWorkspaceRow extends ChatWorkspace {
  threads: ChatThread[]
}

export interface ChatLedger {
  workspaces: ChatWorkspaceRow[]
  threads: ChatThread[]
}

function fail(field: string): never {
  throw new ApiError({
    code: 'protocol_error',
    message: `chat ledger 响应必填字段缺失或类型错误：${field}`,
    retryable: false,
  })
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function reqStr(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

function optNum(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

function optTime(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function parseWorkspace(raw: unknown, ctx: string): ChatWorkspace {
  if (!isObj(raw)) fail(ctx)
  return {
    id: reqStr(raw.id, `${ctx}.id`),
    path: reqStr(raw.path, `${ctx}.path`),
    title: typeof raw.title === 'string' ? raw.title : '',
    created_at: optTime(raw.created_at),
    order: optNum(raw.order),
  }
}

function parseThread(raw: unknown, ctx: string): ChatThread {
  if (!isObj(raw)) fail(ctx)
  return {
    id: reqStr(raw.id, `${ctx}.id`),
    workspace_id: reqStr(raw.workspace_id, `${ctx}.workspace_id`),
    herdr_session: reqStr(raw.herdr_session, `${ctx}.herdr_session`),
    title: typeof raw.title === 'string' ? raw.title : '',
    created_at: optTime(raw.created_at),
  }
}

export async function fetchChatLedger(): Promise<ChatLedger> {
  const raw = await legacyGet('/api/chat/workspaces')
  if (!isObj(raw)) fail('ledger')
  const workspacesRaw = Array.isArray(raw.workspaces) ? raw.workspaces : fail('workspaces')
  const threadsRaw = Array.isArray(raw.threads) ? raw.threads : []
  const threads = threadsRaw.map((row, i) => parseThread(row, `threads[${i}]`))
  const workspaces = workspacesRaw.map((row, i) => {
    const ws = parseWorkspace(row, `workspaces[${i}]`)
    const nested = isObj(row) && Array.isArray(row.threads) ? row.threads : []
    return {
      ...ws,
      threads: nested.map((t, j) => parseThread(t, `workspaces[${i}].threads[${j}]`)),
    }
  })
  return { workspaces, threads }
}

export async function createChatWorkspace(path: string, title?: string): Promise<ChatWorkspace> {
  return parseWorkspace(
    await legacyPost('/api/chat/workspaces', title ? { path, title } : { path }),
    'workspace',
  )
}

export function deleteChatWorkspace(id: string): Promise<unknown> {
  return legacyDelete(`/api/chat/workspaces/${encodeURIComponent(id)}`)
}

export async function createChatThread(
  workspaceId: string,
  herdrSession: string,
  title?: string,
): Promise<ChatThread> {
  return parseThread(
    await legacyPost(
      `/api/chat/workspaces/${encodeURIComponent(workspaceId)}/threads`,
      title ? { herdr_session: herdrSession, title } : { herdr_session: herdrSession },
    ),
    'thread',
  )
}

export interface ChatBindCandidate {
  name: string
  status: string
  directory: string
}

export interface ChatAgentMail {
  ok: boolean
  reason?: string
  error?: string
}

export interface ChatOpenResult {
  thread?: ChatThread
  status?: string
  started?: boolean
  needs_bind?: boolean
  candidates?: ChatBindCandidate[]
  empty?: boolean
  bound?: ChatThread[]
  agent_mail?: ChatAgentMail
}

function parseCandidate(raw: unknown, ctx: string): ChatBindCandidate {
  if (!isObj(raw)) fail(ctx)
  return {
    name: reqStr(raw.name, `${ctx}.name`),
    status: typeof raw.status === 'string' ? raw.status : 'unknown',
    directory: typeof raw.directory === 'string' ? raw.directory : '',
  }
}

export async function bindChatWorkspace(
  workspaceId: string,
  herdrSession: string,
): Promise<ChatThread> {
  const raw = await legacyPost(
    `/api/chat/workspaces/${encodeURIComponent(workspaceId)}/bind`,
    { herdr_session: herdrSession },
  )
  if (isObj(raw) && raw.thread) return parseThread(raw.thread, 'bind.thread')
  return parseThread(raw, 'bind')
}

export async function createChatSession(
  workspaceId: string,
  agent: string,
  title?: string,
  opts?: { model?: string; args?: string },
): Promise<{ session: string; thread: ChatThread }> {
  const body: Record<string, string> = { agent }
  if (title) body.title = title
  if (opts?.model) body.model = opts.model
  if (opts?.args) body.args = opts.args
  const raw = await legacyPost(
    `/api/chat/workspaces/${encodeURIComponent(workspaceId)}/sessions`,
    body,
  )
  if (!isObj(raw)) fail('createSession')
  const session = reqStr(raw.session, 'createSession.session')
  const thread = raw.thread ? parseThread(raw.thread, 'createSession.thread') : parseThread(raw, 'createSession')
  return { session, thread }
}

export async function openChatWorkspace(workspaceId: string): Promise<ChatOpenResult> {
  const raw = await legacyPost(
    `/api/chat/workspaces/${encodeURIComponent(workspaceId)}/open`,
    {},
  )
  if (!isObj(raw)) fail('open')
  const candidates = Array.isArray(raw.candidates)
    ? raw.candidates.map((row, i) => parseCandidate(row, `candidates[${i}]`))
    : []
  const mail = isObj(raw.agent_mail) ? raw.agent_mail : null
  return {
    thread: raw.thread ? parseThread(raw.thread, 'open.thread') : undefined,
    status: typeof raw.status === 'string' ? raw.status : undefined,
    started: raw.started === true,
    needs_bind: raw.needs_bind === true,
    candidates,
    empty: raw.empty === true,
    bound: Array.isArray(raw.bound)
      ? raw.bound.map((row, i) => parseThread(row, `bound[${i}]`))
      : [],
    agent_mail: mail
      ? {
          ok: mail.ok === true,
          reason: typeof mail.reason === 'string' ? mail.reason : undefined,
          error: typeof mail.error === 'string' ? mail.error : undefined,
        }
      : undefined,
  }
}
