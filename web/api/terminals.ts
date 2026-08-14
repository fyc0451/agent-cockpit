// TERM-003 Workspace Terminal v1 客户端（合同：docs/contracts/workspace-terminal-v1.md）。
// 红线：浏览器只提交 project/workspace authority 标识与后端要求的 opaque 参数
// （revision/generation/cursor/cols/rows/Idempotency-Key）；绝不提交 cwd、command、
// argv、PID、FD、env、HOME、SHELL、Herdr session/pane 或内部 terminal ID。
// 纪律与 localSlice.ts 相同：envelope/必填字段/键集 fail-closed（ProtocolError）。

import { ApiError, ProtocolError } from './client'

export const TERMINAL_API = {
  tickets: (projectId: string, workspaceId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/terminal-tickets`,
  ticket: (projectId: string, workspaceId: string, ticketId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/terminal-tickets/${encodeURIComponent(ticketId)}`,
  control: (projectId: string, workspaceId: string, ticketId: string, action: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/terminal-tickets/${encodeURIComponent(ticketId)}/${action}`,
  stream: (projectId: string, workspaceId: string, ticketId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/terminal-tickets/${encodeURIComponent(ticketId)}/stream`,
} as const

// ---------- 类型（合同精确形状） ----------

export type TerminalTicketState =
  | 'pending'
  | 'running'
  | 'exited'
  | 'stopped'
  | 'process_unknown'

/** TerminalTicketStore.public_dict 精确 11 键 */
export interface TerminalTicket {
  ticket_id: string
  project_id: string
  workspace_id: string
  desired_state: string
  observed_state: string
  engine_generation: number
  reconnect_cursor: number
  receipt_refs: { type?: string; id?: string }[]
  revision: number
  created_at: string
  updated_at: string
}

export interface TerminalRuntime {
  state: TerminalTicketState
  replay_available: boolean
  replay_truncated: boolean
}

/** 合同投影：{ticket, runtime} */
export interface TerminalTicketView {
  ticket: TerminalTicket
  runtime: TerminalRuntime
}

export interface TerminalTicketListData {
  items: TerminalTicketView[]
  next_cursor: string | null
}

// ---------- 守卫（fail-closed，同 localSlice 纪律） ----------

function fail(field: string): never {
  throw new ProtocolError(`terminal 响应必填字段缺失或类型错误：${field}`)
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function reqObj(v: unknown, field: string): Record<string, unknown> {
  if (!isObj(v)) fail(field)
  return v
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

function reqBool(v: unknown, field: string): boolean {
  if (typeof v !== 'boolean') fail(field)
  return v
}

/** 正整数（type 精确 number/int，bool 不算；合同 signed-64 fence） */
function reqPositiveInt(v: unknown, field: string): number {
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 1 || !Number.isSafeInteger(v)) {
    fail(field)
  }
  return v
}

function reqNonNegativeInt(v: unknown, field: string): number {
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 0 || !Number.isSafeInteger(v)) {
    fail(field)
  }
  return v
}

function reqExactKeys(o: Record<string, unknown>, keys: readonly string[], ctx: string): void {
  const actual = Object.keys(o).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((k, i) => k !== expected[i])) {
    const missing = expected.filter((k) => !actual.includes(k))
    const extra = actual.filter((k) => !expected.includes(k))
    fail(`${ctx} 键集（缺:${missing.join(',') || '无'} 多:${extra.join(',') || '无'}）`)
  }
}

const TICKET_KEYS = [
  'ticket_id',
  'project_id',
  'workspace_id',
  'desired_state',
  'observed_state',
  'engine_generation',
  'reconnect_cursor',
  'receipt_refs',
  'revision',
  'created_at',
  'updated_at',
] as const

const RUNTIME_STATES: readonly TerminalTicketState[] = [
  'pending',
  'running',
  'exited',
  'stopped',
  'process_unknown',
]

/** Store 冻结状态闭集（terminal_ticket_store._STATES） */
const TICKET_STATES = ['running', 'stopped', 'paused', 'recovery_required', 'unknown'] as const
const RECEIPT_TYPES = ['operation', 'terminal_exit'] as const

export function assertTerminalTicket(raw: unknown, ctx = 'terminal-ticket'): TerminalTicket {
  const o = reqObj(raw, ctx)
  reqExactKeys(o, TICKET_KEYS, ctx)
  if (!Array.isArray(o.receipt_refs)) fail(`${ctx}.receipt_refs`)
  const refs = (o.receipt_refs as unknown[]).map((item, i) => {
    const r = reqObj(item, `${ctx}.receipt_refs[${i}]`)
    reqExactKeys(r, ['type', 'id'], `${ctx}.receipt_refs[${i}]`)
    const kind = r.type
    if (typeof kind !== 'string' || !RECEIPT_TYPES.includes(kind as (typeof RECEIPT_TYPES)[number])) {
      fail(`${ctx}.receipt_refs[${i}].type`)
    }
    return { type: kind, id: reqString(r.id, `${ctx}.receipt_refs[${i}].id`) }
  })
  const desired = reqString(o.desired_state, `${ctx}.desired_state`)
  const observed = reqString(o.observed_state, `${ctx}.observed_state`)
  if (!TICKET_STATES.includes(desired as (typeof TICKET_STATES)[number])) fail(`${ctx}.desired_state 枚举`)
  if (!TICKET_STATES.includes(observed as (typeof TICKET_STATES)[number])) fail(`${ctx}.observed_state 枚举`)
  return {
    ticket_id: reqString(o.ticket_id, `${ctx}.ticket_id`),
    project_id: reqString(o.project_id, `${ctx}.project_id`),
    workspace_id: reqString(o.workspace_id, `${ctx}.workspace_id`),
    desired_state: desired,
    observed_state: observed,
    engine_generation: reqPositiveInt(o.engine_generation, `${ctx}.engine_generation`),
    reconnect_cursor: reqNonNegativeInt(o.reconnect_cursor, `${ctx}.reconnect_cursor`),
    receipt_refs: refs,
    revision: reqPositiveInt(o.revision, `${ctx}.revision`),
    created_at: reqString(o.created_at, `${ctx}.created_at`),
    updated_at: reqString(o.updated_at, `${ctx}.updated_at`),
  }
}

export function assertTerminalTicketView(raw: unknown, ctx = 'terminal-ticket-view'): TerminalTicketView {
  const o = reqObj(raw, ctx)
  reqExactKeys(o, ['ticket', 'runtime'], ctx)
  const runtime = reqObj(o.runtime, `${ctx}.runtime`)
  reqExactKeys(runtime, ['state', 'replay_available', 'replay_truncated'], `${ctx}.runtime`)
  const state = runtime.state
  if (typeof state !== 'string' || !RUNTIME_STATES.includes(state as TerminalTicketState)) {
    fail(`${ctx}.runtime.state`)
  }
  return {
    ticket: assertTerminalTicket(o.ticket, `${ctx}.ticket`),
    runtime: {
      state: state as TerminalTicketState,
      replay_available: reqBool(runtime.replay_available, `${ctx}.runtime.replay_available`),
      replay_truncated: reqBool(runtime.replay_truncated, `${ctx}.runtime.replay_truncated`),
    },
  }
}

export function assertTerminalTicketListData(raw: unknown): TerminalTicketListData {
  const o = reqObj(raw, 'terminal-tickets')
  reqExactKeys(o, ['items', 'next_cursor'], 'terminal-tickets')
  if (!Array.isArray(o.items)) fail('terminal-tickets.items')
  if (o.next_cursor !== null && typeof o.next_cursor !== 'string') fail('terminal-tickets.next_cursor')
  return {
    items: (o.items as unknown[]).map((item, i) => assertTerminalTicketView(item, `terminal-tickets.items[${i}]`)),
    next_cursor: (o.next_cursor ?? null) as string | null,
  }
}

// ---------- HTTP（G3 envelope；POST 带 Idempotency-Key 的合同路由必须持 key） ----------

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    retryable?: boolean
    request_id?: string
    details?: unknown
  } | null
}

function codeForStatus(status: number): string {
  if (status === 400) return 'invalid_argument'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status >= 500) return 'server_error'
  return 'http_error'
}

async function request<T>(method: string, path: string, body?: Record<string, unknown>, idempotencyKey?: string): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(idempotencyKey !== undefined ? { 'Idempotency-Key': idempotencyKey } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }

  let parsed: unknown = null
  let jsonOk = true
  try {
    parsed = await res.json()
  } catch {
    jsonOk = false
  }

  if (isObj(parsed) && (parsed as ErrorEnvelope).error) {
    const e = (parsed as ErrorEnvelope).error!
    throw new ApiError({
      code: e.code ?? codeForStatus(res.status),
      message: e.message ?? `请求失败（HTTP ${res.status}）`,
      retryable: e.retryable ?? res.status >= 500,
      requestId: e.request_id ?? null,
      status: res.status,
      details: e.details,
    })
  }
  if (!res.ok) {
    throw new ApiError({
      code: codeForStatus(res.status),
      message: `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }
  if (!jsonOk) throw new ProtocolError('响应不是合法 JSON', { status: res.status })
  if (!isObj(parsed)) throw new ProtocolError('响应不是 G3 envelope（裸 body 不允许透传）', { status: res.status })
  if (!('data' in parsed)) throw new ProtocolError('响应 envelope 缺少 data 键', { status: res.status })
  if (!('meta' in parsed)) throw new ProtocolError('响应 envelope 缺少 meta 键', { status: res.status })
  // 顶层 exact 闭集 {data,meta}；meta 必须是对象（拒绝额外顶层键与非对象 meta）
  const topKeys = Object.keys(parsed).sort()
  if (topKeys.length !== 2 || topKeys[0] !== 'data' || topKeys[1] !== 'meta') {
    throw new ProtocolError('响应 envelope 顶层键集必须是精确 {data,meta}', { status: res.status })
  }
  if (!isObj((parsed as Record<string, unknown>).meta)) {
    throw new ProtocolError('响应 envelope meta 必须是对象', { status: res.status })
  }
  return (parsed as { data: T }).data
}

/** 合同：create/control 幂等键 1-128 可见 ASCII */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `ttk-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 18)}`
}

export function listTerminalTickets(projectId: string, workspaceId: string): Promise<TerminalTicketListData> {
  return request<unknown>('GET', TERMINAL_API.tickets(projectId, workspaceId)).then((raw) =>
    assertTerminalTicketListData(raw),
  )
}

export function getTerminalTicket(projectId: string, workspaceId: string, ticketId: string): Promise<TerminalTicketView> {
  return request<unknown>('GET', TERMINAL_API.ticket(projectId, workspaceId, ticketId)).then((raw) =>
    assertTerminalTicketView(raw),
  )
}

/** 创建：body 精确 {revision, cols, rows}（revision = Workspace version fence） */
export function createTerminalTicket(
  projectId: string,
  workspaceId: string,
  revision: number,
  cols: number,
  rows: number,
  idempotencyKey: string,
): Promise<TerminalTicketView> {
  return request<unknown>('POST', TERMINAL_API.tickets(projectId, workspaceId), { revision, cols, rows }, idempotencyKey).then(
    (raw) => assertTerminalTicketView(raw),
  )
}

export function interruptTerminalTicket(
  ids: { projectId: string; workspaceId: string; ticketId: string },
  fence: { revision: number; generation: number },
  idempotencyKey: string,
): Promise<TerminalTicketView> {
  return request<unknown>(
    'POST',
    TERMINAL_API.control(ids.projectId, ids.workspaceId, ids.ticketId, 'interrupt'),
    { revision: fence.revision, generation: fence.generation },
    idempotencyKey,
  ).then((raw) => assertTerminalTicketView(raw))
}

/** 重连：body 精确 {revision, generation, cursor, cols, rows}（合同无幂等键） */
export function reconnectTerminalTicket(
  ids: { projectId: string; workspaceId: string; ticketId: string },
  fence: { revision: number; generation: number; cursor: number },
  dims: { cols: number; rows: number },
): Promise<TerminalTicketView> {
  return request<unknown>('POST', TERMINAL_API.control(ids.projectId, ids.workspaceId, ids.ticketId, 'reconnect'), {
    revision: fence.revision,
    generation: fence.generation,
    cursor: fence.cursor,
    cols: dims.cols,
    rows: dims.rows,
  }).then((raw) => assertTerminalTicketView(raw))
}

export function restartTerminalTicket(
  ids: { projectId: string; workspaceId: string; ticketId: string },
  fence: { revision: number; generation: number },
  dims: { cols: number; rows: number },
  idempotencyKey: string,
): Promise<TerminalTicketView> {
  return request<unknown>(
    'POST',
    TERMINAL_API.control(ids.projectId, ids.workspaceId, ids.ticketId, 'restart'),
    { revision: fence.revision, generation: fence.generation, cols: dims.cols, rows: dims.rows },
    idempotencyKey,
  ).then((raw) => assertTerminalTicketView(raw))
}

export function closeTerminalTicket(
  ids: { projectId: string; workspaceId: string; ticketId: string },
  fence: { revision: number; generation: number },
  idempotencyKey: string,
): Promise<TerminalTicketView> {
  return request<unknown>(
    'POST',
    TERMINAL_API.control(ids.projectId, ids.workspaceId, ids.ticketId, 'close'),
    { revision: fence.revision, generation: fence.generation },
    idempotencyKey,
  ).then((raw) => assertTerminalTicketView(raw))
}

// ---------- WebSocket 流（合同：URL 无 query；首帧精确 attach；随后仅 input/resize） ----------

/** 应用关闭码（合同闭集 + 框架 1008） */
export const STREAM_CLOSE = {
  UNAUTHORIZED: 1008,
  INVALID: 4400,
  NOT_FOUND: 4404,
  CONFLICT: 4409,
  UNAVAILABLE: 4503,
} as const

export interface TerminalStreamFence {
  revision: number
  generation: number
  cursor: number
}

export interface TerminalStreamHandlers {
  onReplayStart: () => void
  /** 有界二进制 replay 历史，随后是 live 输出（同一回调，按时序） */
  onData: (data: Uint8Array) => void
  onReplayComplete: (truncated: boolean) => void
  onExit: (generation: number) => void
  onError: (code: string) => void
  /** server 帧违反合同（键集/时序/fence/乱序）：stream 已被客户端关闭，stdin 保持关闭 */
  onProtocolError: (why: string) => void
  /** 任何关闭都回报（含正常关闭）；code 见 STREAM_CLOSE */
  onClose: (code: number, reason: string) => void
}

export interface TerminalStream {
  sendInput: (value: string) => void
  sendResize: (cols: number, rows: number) => void
  close: () => void
  readonly ready: boolean
}

/** 由当前页面 location 推导 ws/wss URL（不接收外部 host，不附加 query） */
export function terminalStreamUrl(projectId: string, workspaceId: string, ticketId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${TERMINAL_API.stream(projectId, workspaceId, ticketId)}`
}

export function connectTerminalStream(
  ids: { projectId: string; workspaceId: string; ticketId: string },
  fence: TerminalStreamFence,
  handlers: TerminalStreamHandlers,
): TerminalStream {
  const ws = new WebSocket(terminalStreamUrl(ids.projectId, ids.workspaceId, ids.ticketId))
  ws.binaryType = 'arraybuffer'
  let open = false
  let closed = false

  const sendFrame = (frame: Record<string, unknown>) => {
    if (!open || closed || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify(frame))
  }

  ws.onopen = () => {
    open = true
    // 首帧精确 {type:"attach",revision,generation,cursor}
    ws.send(
      JSON.stringify({
        type: 'attach',
        revision: fence.revision,
        generation: fence.generation,
        cursor: fence.cursor,
      }),
    )
  }
  // P1-1：server 控制帧 exact-key + 严格时序 + fence 校验；任一非法/乱序帧 →
  // 协议失败（关闭该 stream，stdin 保持关闭），绝不宽容放行。
  type ServerPhase = 'awaiting_replay_start' | 'replaying' | 'live'
  let serverPhase: ServerPhase = 'awaiting_replay_start'
  let protocolFailed = false
  const protocolError = (why: string) => {
    if (protocolFailed) return
    protocolFailed = true
    closed = true
    try {
      ws.close()
    } catch {
      /* 已关闭 */
    }
    handlers.onProtocolError(why)
  }
  const exactKeys = (frame: Record<string, unknown>, keys: readonly string[]): boolean => {
    const actual = Object.keys(frame).sort()
    const expected = [...keys].sort()
    return actual.length === expected.length && actual.every((k, i) => k === expected[i])
  }
  const fenceMatch = (frame: Record<string, unknown>): boolean =>
    frame.revision === fence.revision &&
    frame.generation === fence.generation &&
    frame.cursor === fence.cursor
  const FENCE_KEYS = ['type', 'revision', 'generation', 'cursor'] as const

  ws.onmessage = (event: MessageEvent) => {
    if (protocolFailed) return
    if (typeof event.data !== 'string') {
      // 二进制帧只允许出现在 replay_start 之后（replay 历史或 live 输出）；
      // 跨 realm 安全：不做 instanceof ArrayBuffer
      if (serverPhase === 'awaiting_replay_start') {
        protocolError('unexpected_binary_before_replay_start')
        return
      }
      try {
        const bytes = new Uint8Array(event.data as ArrayBuffer)
        if (bytes.length > 0) handlers.onData(bytes)
      } catch {
        protocolError('invalid_binary_payload')
      }
      return
    }
    let frame: unknown
    try {
      frame = JSON.parse(event.data)
    } catch {
      protocolError('invalid_json_frame')
      return
    }
    if (!isObj(frame) || typeof frame.type !== 'string') {
      protocolError('invalid_frame_shape')
      return
    }
    switch (frame.type) {
      case 'replay_start':
        if (serverPhase !== 'awaiting_replay_start') {
          protocolError('out_of_order_replay_start')
          return
        }
        if (!exactKeys(frame, FENCE_KEYS) || !fenceMatch(frame)) {
          protocolError('replay_start_keys_or_fence')
          return
        }
        serverPhase = 'replaying'
        handlers.onReplayStart()
        break
      case 'replay_complete':
        if (serverPhase !== 'replaying') {
          protocolError('out_of_order_replay_complete')
          return
        }
        if (
          !exactKeys(frame, [...FENCE_KEYS, 'truncated']) ||
          !fenceMatch(frame) ||
          typeof frame.truncated !== 'boolean'
        ) {
          protocolError('replay_complete_keys_or_fence')
          return
        }
        serverPhase = 'live'
        handlers.onReplayComplete(frame.truncated)
        break
      case 'exit':
        if (serverPhase === 'awaiting_replay_start') {
          protocolError('out_of_order_exit')
          return
        }
        if (!exactKeys(frame, ['type', 'generation']) || frame.generation !== fence.generation) {
          protocolError('exit_keys_or_generation')
          return
        }
        handlers.onExit(fence.generation)
        break
      case 'error':
        if (!exactKeys(frame, ['type', 'code']) || typeof frame.code !== 'string' || frame.code === '') {
          protocolError('error_frame_shape')
          return
        }
        handlers.onError(frame.code)
        break
      default:
        protocolError('unknown_frame_type')
        break
    }
  }
  ws.onclose = (event: CloseEvent) => {
    closed = true
    handlers.onClose(event.code, event.reason ?? '')
  }
  ws.onerror = () => {
    // 错误细节由随后的 close 事件携带（浏览器不暴露更多）
  }

  return {
    sendInput(value: string) {
      sendFrame({
        type: 'input',
        revision: fence.revision,
        generation: fence.generation,
        cursor: fence.cursor,
        input: value,
      })
    },
    sendResize(cols: number, rows: number) {
      sendFrame({
        type: 'resize',
        revision: fence.revision,
        generation: fence.generation,
        cursor: fence.cursor,
        cols,
        rows,
      })
    },
    close() {
      closed = true
      try {
        ws.close()
      } catch {
        /* 已关闭 */
      }
    },
    get ready() {
      return open && !closed
    },
  }
}
