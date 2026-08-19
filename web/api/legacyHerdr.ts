// 群聊工作台：legacy herdr 接口客户端（裸 JSON，非 G3 {data,meta} envelope）。
// 纪律：
// - GET 复用 localSlice.legacyGet 的裸 body 约定与错误映射；POST 在本文件内实现同等映射
//   （FastAPI HTTPException → {detail}；网络失败 → disconnected）。
// - 形状守卫 = 必需键 fail-closed + 多余键宽容：legacy 契约未冻结，且服务端会对
//   snapshot 做键富化（_enrich_board_identities），exactKeys 会误伤合法响应。

import { noteAuthFailure } from './authEvents'
import { ApiError } from './client'
import { legacyGet } from './localSlice'

// ---------- 类型 ----------

export interface HerdrSession {
  name: string
  status: string // running | stopped
  directory: string
  socket: string
}

/** herdr snapshot 里的一个 pane；agent='' 表示非 agent pane（如 shell） */
export interface HerdrPane {
  pane_id: string
  session: string
  agent: string
  agent_status: string // idle | working | blocked | done | unknown
  cwd: string
  cwd_name: string
  display_name: string
  mail_name: string
  tab_id: string
  focused: boolean
  turn_started_ms?: number
  activity?: string
  unread?: number
}

export interface SessionLeader {
  mail_name: string
  agent?: string
}

export interface HerdrSnapshot {
  panes: HerdrPane[]
  session_leaders?: Record<string, SessionLeader>
}

export interface PaneSummary {
  available: boolean
  summary: string
  error: string | null
}

export interface SetupParticipant {
  agent: string
  name: string
  role: string
  task?: string
}

export interface SetupWorkspaceRequest {
  session: string
  workdir: string
  agents: string[]
  layout: 'tab'
  mode: 'quick'
  participants: SetupParticipant[]
}

export interface SetupWorkspaceResult {
  ok?: boolean
  session?: string
  error?: string
  started_instances?: string[]
  started?: string[]
  reused_instances?: string[]
  reused?: string[]
  registered?: boolean
}

export interface StartAgentRequest {
  session: string
  workdir: string
  agent: string
  name?: string
  model?: string
  layout: 'tab'
  workspace: 'shared'
  args?: string
}

export interface StartAgentResult {
  ok?: boolean
  pane_id?: string
  error?: string
  started?: boolean
}

export interface HerdrTerminal {
  id: string
  label: string
}

// ---------- 守卫 ----------

function fail(field: string): never {
  throw new ApiError({
    code: 'protocol_error',
    message: `legacy herdr 响应必填字段缺失或类型错误：${field}`,
    retryable: false,
  })
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function optStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function reqStr(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

function optNonNegInt(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : undefined
}

function assertPane(raw: unknown, ctx: string): HerdrPane {
  if (!isObj(raw)) fail(ctx)
  const activity = optStr(raw.activity)
  return {
    pane_id: reqStr(raw.pane_id, `${ctx}.pane_id`),
    session: optStr(raw.session),
    agent: optStr(raw.agent),
    agent_status: optStr(raw.agent_status) || 'unknown',
    cwd: optStr(raw.cwd),
    cwd_name: optStr(raw.cwd_name),
    display_name: optStr(raw.display_name),
    mail_name: optStr(raw.mail_name),
    tab_id: optStr(raw.tab_id),
    focused: raw.focused === true,
    turn_started_ms: optNonNegInt(raw.turn_started_ms),
    activity: activity || undefined,
    unread: optNonNegInt(raw.unread),
  }
}

function assertSessionLeaders(raw: unknown): Record<string, SessionLeader> | undefined {
  if (!isObj(raw)) return undefined
  const out: Record<string, SessionLeader> = {}
  for (const [session, row] of Object.entries(raw)) {
    if (!session || !isObj(row)) continue
    const mail = optStr(row.mail_name).trim()
    if (!mail) continue
    const agent = optStr(row.agent).trim()
    out[session] = agent ? { mail_name: mail, agent } : { mail_name: mail }
  }
  return Object.keys(out).length > 0 ? out : undefined
}

function assertSnapshot(raw: unknown): HerdrSnapshot {
  if (!isObj(raw) || !Array.isArray(raw.panes)) fail('snapshot.panes')
  return {
    panes: raw.panes.map((p, i) => assertPane(p, `panes[${i}]`)),
    session_leaders: assertSessionLeaders(raw.session_leaders),
  }
}

function assertSessions(raw: unknown): HerdrSession[] {
  if (!isObj(raw) || !Array.isArray(raw.sessions)) fail('sessions')
  return raw.sessions.map((row, i) => {
    if (!isObj(row)) fail(`sessions[${i}]`)
    return {
      name: reqStr(row.name, `sessions[${i}].name`),
      status: optStr(row.status) || 'unknown',
      directory: optStr(row.directory),
      socket: optStr(row.socket),
    }
  })
}

function assertSummary(raw: unknown): PaneSummary {
  if (!isObj(raw)) fail('summary')
  return {
    available: raw.available !== false,
    summary: optStr(raw.summary),
    error: typeof raw.error === 'string' ? raw.error : null,
  }
}

// ---------- POST（legacy 裸 JSON 错误映射，与 localSlice.legacyGet 对齐） ----------

export async function legacyPost<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  return legacySend<TRes>(path, 'POST', body)
}

export async function legacyDelete<TRes>(path: string): Promise<TRes> {
  return legacySend<TRes>(path, 'DELETE')
}

async function legacySend<TRes>(path: string, method: 'POST' | 'DELETE', body?: unknown): Promise<TRes> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
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
    const error = new ApiError({
      code: res.status === 401
        ? 'unauthenticated'
        : res.status === 409 ? 'conflict' : res.status >= 500 ? 'server_error' : 'http_error',
      message: detail ?? `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
    noteAuthFailure(error)
    throw error
  }
  return parsed as TRes
}

// ---------- 接口 ----------

export async function fetchHerdrStatus(): Promise<{
  available: boolean
  scopedSession: string | null // next profile 单会话作用域；非 null 时只能用这个会话名
}> {
  const raw = await legacyGet('/api/herdr/status')
  if (!isObj(raw)) fail('status')
  return {
    available: raw.available === true,
    scopedSession: typeof raw.scoped_session === 'string' && raw.scoped_session ? raw.scoped_session : null,
  }
}

export async function fetchHerdrSessions(): Promise<HerdrSession[]> {
  return assertSessions(await legacyGet('/api/herdr/sessions'))
}

export async function fetchHerdrSnapshot(): Promise<HerdrSnapshot> {
  return assertSnapshot(await legacyGet('/api/herdr/snapshot'))
}

export async function fetchPaneSummary(
  session: string,
  paneId: string,
  maxLines = 60,
): Promise<PaneSummary> {
  const path =
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}` +
    `/summary?max_lines=${maxLines}`
  return assertSummary(await legacyGet(path))
}

/** 往 agent pane 发 prompt（群聊发送语义）；mode=prompt 走 agent 输入而非裸按键 */
export function sendPanePrompt(
  session: string,
  paneId: string,
  text: string,
): Promise<unknown> {
  return sendPane(session, paneId, text, 'prompt')
}

export function sendPane(
  session: string,
  paneId: string,
  text: string,
  mode: 'prompt' | 'send' | 'keys' | 'slash',
): Promise<unknown> {
  return legacyPost(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}/send`,
    { text, mode },
  )
}

export async function fetchPaneOutput(
  session: string,
  paneId: string,
  lines = 80,
): Promise<{ output: string; error: string | null }> {
  const raw = await legacyGet(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}` +
      `?lines=${lines}&is_agent=1`,
  )
  if (!isObj(raw)) fail('pane')
  return {
    output: extractPaneText(raw),
    error: typeof raw.error === 'string' && raw.error ? raw.error : null,
  }
}

function extractPaneText(raw: Record<string, unknown>): string {
  const output = raw.output
  if (typeof output !== 'string' || output.trim() === '') {
    return typeof raw.error === 'string' ? raw.error : ''
  }
  const trimmed = output.trim()
  if (!trimmed.startsWith('{')) return output
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (!isObj(parsed)) return output
    const result = isObj(parsed.result) ? parsed.result : parsed
    for (const key of ['text', 'output', 'content']) {
      const value = result[key]
      if (typeof value === 'string' && value.trim()) return value
    }
  } catch {
    return output
  }
  return output
}

/** 创建会话 = 建 herdr session + 首个 participant（leader）启动 */
export function setupWorkspace(req: SetupWorkspaceRequest): Promise<SetupWorkspaceResult> {
  return legacyPost('/api/herdr/setup-workspace', req)
}

function assertHerdrMutation(raw: unknown, action: string): void {
  if (!isObj(raw)) return
  if (typeof raw.error === 'string' && raw.error) {
    throw new ApiError({
      code: 'herdr_error',
      message: raw.error,
      retryable: false,
    })
  }
  if (raw.available === false) {
    throw new ApiError({
      code: 'herdr_unavailable',
      message: `herdr 不可用，无法${action}`,
      retryable: true,
    })
  }
}

/** herdr session stop：已停或 socket 不可达，删除时应继续。 */
export function isAlreadyStoppedError(error: unknown): boolean {
  const text = error instanceof ApiError ? error.message : String(error ?? '')
  return /is not running or cannot be reached|session_stop_failed/i.test(text)
}

/** 停止 herdr session。进程没了，名字还在，以后可以再 start。 */
export async function stopHerdrSession(name: string): Promise<unknown> {
  const raw = await legacyPost(`/api/herdr/session/${encodeURIComponent(name)}/stop`, {})
  assertHerdrMutation(raw, '停止会话')
  return raw
}

/** 删除已停止的 herdr session。若仍在跑，调用方应先 stop。 */
export async function deleteHerdrSession(name: string): Promise<unknown> {
  const raw = await legacyDelete(`/api/herdr/session/${encodeURIComponent(name)}`)
  assertHerdrMutation(raw, '删除会话')
  return raw
}

/** 会话内添加成员 = session 内新开 tab 启动一个 agent */
export function startAgent(req: StartAgentRequest): Promise<StartAgentResult> {
  return legacyPost('/api/herdr/start', req)
}

/** 关闭群成员 pane；成功后花名/身份一并清掉。 */
export async function closePane(session: string, paneId: string): Promise<unknown> {
  const raw = await legacyDelete(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}`,
  )
  assertHerdrMutation(raw, '关闭成员')
  return raw
}

/** 原位重启群成员；不清花名，只打断当前任务再拉起来。 */
export async function restartPane(session: string, paneId: string): Promise<unknown> {
  const raw = await legacyPost(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}/restart`,
    {},
  )
  assertHerdrMutation(raw, '重启成员')
  return raw
}

/** 打开指定群聊的 Herdr TUI；命令和工作目录均由后端固定。 */
export async function openHerdrTerminal(
  session: string,
  cols: number,
  rows: number,
): Promise<HerdrTerminal> {
  const raw = await legacyPost(
    `/api/chat/sessions/${encodeURIComponent(session)}/terminal?cols=${cols}&rows=${rows}`,
    {},
  )
  if (!isObj(raw)) fail('terminal')
  return {
    id: reqStr(raw.id, 'terminal.id'),
    label: reqStr(raw.label, 'terminal.label'),
  }
}

export async function closeHerdrTerminal(termId: string): Promise<void> {
  await legacyDelete(`/api/term/${encodeURIComponent(termId)}`)
}

export function herdrTerminalWebSocketUrl(termId: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/api/term/${encodeURIComponent(termId)}?replay=1`
}

export function paneLiveWebSocketUrl(session: string, paneId: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return (
    `${scheme}//${window.location.host}` +
    `/api/chat/sessions/${encodeURIComponent(session)}` +
    `/panes/${encodeURIComponent(paneId)}/live`
  )
}

export function composeSessionLayout(
  session: string,
  paneIds: string[],
  orientation: 'horizontal' | 'vertical',
): Promise<unknown> {
  return legacyPost(`/api/herdr/session/${encodeURIComponent(session)}/layout/compose`, {
    pane_ids: paneIds,
    orientation,
  })
}

export function splitPaneLayout(
  session: string,
  paneId: string,
  mode: 'horizontal' | 'vertical' | 'grid4',
): Promise<unknown> {
  return legacyPost(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}/layout/split`,
    { mode },
  )
}

export function detachPaneLayout(session: string, paneId: string): Promise<unknown> {
  return legacyPost(
    `/api/herdr/pane/${encodeURIComponent(session)}/${encodeURIComponent(paneId)}/layout/detach`,
    {},
  )
}

export function untileSessionLayout(session: string, tabId: string): Promise<unknown> {
  return legacyPost(`/api/herdr/session/${encodeURIComponent(session)}/layout/untile`, {
    tab_id: tabId,
  })
}
