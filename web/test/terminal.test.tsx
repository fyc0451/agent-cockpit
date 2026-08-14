import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Terminal } from '@xterm/xterm'
import {
  assertTerminalTicketView,
  connectTerminalStream,
  createTerminalTicket,
  type TerminalTicketView,
} from '../api/terminals'
import { defaultFetchMap, metaOk, REG_P1, workspaceW1 } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

// ---------- 可控 fake WebSocket ----------

class FakeWebSocket {
  static OPEN = 1
  static instances: FakeWebSocket[] = []

  readyState = 0
  binaryType = ''
  sent: string[] = []
  closedWith: { code: number; reason: string } | null = null
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number; reason: string }) => void) | null = null
  onerror: (() => void) | null = null

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
    queueMicrotask(() => {
      if (this.readyState !== 0) return
      this.readyState = 1
      this.onopen?.()
    })
  }

  send(data: string) {
    this.sent.push(String(data))
  }

  close(code = 1000, reason = '') {
    if (this.readyState === 3) return
    this.readyState = 3
    this.closedWith = { code, reason }
    queueMicrotask(() => this.onclose?.({ code, reason }))
  }

  serverJson(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  serverRaw(text: string) {
    this.onmessage?.({ data: text })
  }

  serverBytes(text: string) {
    this.onmessage?.({ data: new TextEncoder().encode(text).buffer })
  }

  serverClose(code: number, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }

  lastFrame(): Record<string, unknown> {
    return JSON.parse(this.sent[this.sent.length - 1])
  }
}

// ---------- fixtures ----------

interface FetchCall {
  url: string
  method: string
  body?: Record<string, unknown>
  headers: Record<string, string>
}

const TICKET_ID = `ttk_${'c'.repeat(32)}`
const TICKET_ID_2 = `ttk_${'d'.repeat(32)}`

function ticketView(
  runtime: Partial<TerminalTicketView['runtime']> = {},
  ticket: Record<string, unknown> = {},
): TerminalTicketView {
  return {
    ticket: {
      ticket_id: TICKET_ID,
      project_id: REG_P1,
      workspace_id: 'w1',
      desired_state: 'running',
      observed_state: 'running',
      engine_generation: 1,
      reconnect_cursor: 0,
      receipt_refs: [],
      revision: 1,
      created_at: '2026-08-14T00:00:00+00:00',
      updated_at: '2026-08-14T00:00:00+00:00',
      ...ticket,
    },
    runtime: { state: 'running', replay_available: true, replay_truncated: false, ...runtime },
  }
}

const capsPtyOpenMeta = {
  ...metaOk,
  capabilities: { 'terminal.pty': { available: true, reason: null } },
}

const EMPTY_LIST = { body: { data: { items: [], next_cursor: null }, meta: metaOk } }

interface RouteSpec {
  status?: number
  body: unknown
}

interface LiveRoutes {
  list?: RouteSpec
  /** 静态 spec 或按第 n 次 POST 分派（用于首败后成的重试场景） */
  create?: RouteSpec | ((attempt: number) => RouteSpec | undefined)
  detail?: (ticketId: string) => RouteSpec | undefined
  control?: (action: string) => RouteSpec | undefined
}

/** live 世界：workspace detail 带 terminal.pty=true；terminal-tickets 路由按 method/action 分支 */
async function renderLive(routes: LiveRoutes) {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const calls: FetchCall[] = []
  const map = {
    ...defaultFetchMap(),
    [`/api/project-registry/projects/${REG_P1}/workspaces/w1`]: {
      data: workspaceW1,
      meta: capsPtyOpenMeta,
    },
  }
  const base = `/api/projects/${REG_P1}/workspaces/w1/terminal-tickets`
  let createAttempts = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    let spec: RouteSpec | undefined
    if (url === base || url.startsWith(`${base}/`)) {
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      })
      const rest = url.slice(base.length + 1)
      if (url === base) {
        if (method === 'POST') {
          createAttempts += 1
          spec = typeof routes.create === 'function' ? routes.create(createAttempts) : routes.create
        } else {
          spec = routes.list
        }
      } else if (!rest.includes('/')) {
        spec = routes.detail?.(rest)
      } else {
        spec = routes.control?.(rest.split('/').pop()!)
      }
    } else {
      const key = Object.keys(map)
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      spec = key ? { body: map[key] } : undefined
    }
    if (!spec) {
      return {
        ok: false,
        status: 404,
        json: async () => ({ error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } }),
      } as Response
    }
    const status = spec.status ?? 200
    return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
  }))
  const rendered = renderApp('/projects/p1/workspaces/w1/terminal')
  return { ...rendered, calls }
}

/** attach 并完成 replay，进入 live（帧注入包 act，杜绝早读 DOM 的 false-green） */
async function driveToLive(ws: FakeWebSocket, fence = { revision: 1, generation: 1, cursor: 0 }) {
  await waitFor(() => expect(ws.sent).toHaveLength(1))
  expect(ws.lastFrame()).toEqual({ type: 'attach', ...fence })
  act(() => {
    ws.serverJson({ type: 'replay_start', ...fence })
    ws.serverJson({ type: 'replay_complete', ...fence, truncated: false })
  })
}

beforeEach(() => {
  FakeWebSocket.instances = []
})

// ---------- 单元：守卫与帧形状 ----------

describe('terminals API 守卫与 WS 帧', () => {
  it('ticket view 缺键/错型 fail-closed', () => {
    const good = ticketView()
    expect(assertTerminalTicketView(JSON.parse(JSON.stringify(good)))).toBeTruthy()
    const missing = JSON.parse(JSON.stringify(good)) as Record<string, unknown>
    delete (missing.ticket as Record<string, unknown>).revision
    expect(() => assertTerminalTicketView(missing)).toThrow(/缺:revision/)
    const badState = JSON.parse(JSON.stringify(good))
    badState.runtime.state = 'flying'
    expect(() => assertTerminalTicketView(badState)).toThrow(/state/)
    const extra = JSON.parse(JSON.stringify(good))
    extra.runtime.hacker = 1
    expect(() => assertTerminalTicketView(extra)).toThrow(/键集/)
  })

  it('create body 精确 {revision,cols,rows} + Idempotency-Key；不含 cwd/command/env/pid', async () => {
    const captured: FetchCall[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      captured.push({
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      })
      return { ok: true, status: 201, json: async () => ({ data: ticketView(), meta: metaOk }) } as Response
    }))
    await createTerminalTicket(REG_P1, 'w1', 3, 120, 30, 'idem-1')
    expect(captured).toHaveLength(1)
    const call = captured[0]
    expect(call.method).toBe('POST')
    expect(call.url).toBe(`/api/projects/${REG_P1}/workspaces/w1/terminal-tickets`)
    expect(call.body).toEqual({ revision: 3, cols: 120, rows: 30 })
    expect(call.headers['Idempotency-Key']).toBe('idem-1')
    const raw = JSON.stringify(call.body)
    for (const banned of ['cwd', 'command', 'argv', 'env', 'pid', 'HOME', 'SHELL', 'herdr', 'pane']) {
      expect(raw).not.toContain(banned)
    }
  })

  it('WS 首帧精确 attach；input/resize 帧带同一 fence；URL 无 query', async () => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const stream = connectTerminalStream(
      { projectId: REG_P1, workspaceId: 'w1', ticketId: TICKET_ID },
      { revision: 2, generation: 4, cursor: 9 },
      {
        onReplayStart: () => {},
        onData: () => {},
        onReplayComplete: () => {},
        onExit: () => {},
        onError: () => {},
        onProtocolError: () => {},
        onClose: () => {},
      },
    )
    const ws = FakeWebSocket.instances[0]
    expect(ws.url).toBe(
      `ws://${window.location.host}/api/projects/${REG_P1}/workspaces/w1/terminal-tickets/${TICKET_ID}/stream`,
    )
    expect(ws.url).not.toContain('?')
    await waitFor(() => expect(ws.sent).toHaveLength(1))
    expect(ws.lastFrame()).toEqual({ type: 'attach', revision: 2, generation: 4, cursor: 9 })
    stream.sendInput('ls\n')
    expect(ws.lastFrame()).toEqual({ type: 'input', revision: 2, generation: 4, cursor: 9, input: 'ls\n' })
    stream.sendResize(100, 40)
    expect(ws.lastFrame()).toEqual({ type: 'resize', revision: 2, generation: 4, cursor: 9, cols: 100, rows: 40 })
    stream.close()
  })
})

function stubStream(handlers: Partial<Record<string, (...args: never[]) => void>> = {}) {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  const calls = { protocol: [] as string[], data: 0, live: 0, exit: 0, error: [] as string[] }
  const stream = connectTerminalStream(
    { projectId: REG_P1, workspaceId: 'w1', ticketId: TICKET_ID },
    { revision: 2, generation: 4, cursor: 9 },
    {
      onReplayStart: () => {},
      onData: () => {
        calls.data += 1
      },
      onReplayComplete: () => {
        calls.live += 1
      },
      onExit: () => {
        calls.exit += 1
      },
      onError: (code: string) => {
        calls.error.push(code)
      },
      onProtocolError: (why: string) => {
        calls.protocol.push(why)
      },
      onClose: () => {},
      ...handlers,
    } as never,
  )
  return { stream, ws: FakeWebSocket.instances[0], calls }
}

// ---------- P1-1：server 控制帧 exact/时序/fence（stream 级负例） ----------

describe('WS server 帧 exact decoder（P1-1）', () => {
  async function openedWs() {
    const { stream, ws, calls } = stubStream()
    await waitFor(() => expect(ws.sent).toHaveLength(1))
    return { stream, ws, calls }
  }

  it('replay_complete 先于 replay_start → 协议失败，live 不开放', async () => {
    const { ws, calls } = await openedWs()
    ws.serverJson({ type: 'replay_complete', revision: 2, generation: 4, cursor: 9, truncated: false })
    expect(calls.protocol).toEqual(['out_of_order_replay_complete'])
    expect(calls.live).toBe(0)
  })

  it('replay_complete 带 extra key → 协议失败', async () => {
    const { ws, calls } = await openedWs()
    ws.serverJson({ type: 'replay_start', revision: 2, generation: 4, cursor: 9 })
    ws.serverJson({ type: 'replay_complete', revision: 2, generation: 4, cursor: 9, truncated: false, extra: 1 })
    expect(calls.protocol).toEqual(['replay_complete_keys_or_fence'])
    expect(calls.live).toBe(0)
  })

  it('replay_start 错 fence → 协议失败', async () => {
    const { ws, calls } = await openedWs()
    ws.serverJson({ type: 'replay_start', revision: 99, generation: 4, cursor: 9 })
    expect(calls.protocol).toEqual(['replay_start_keys_or_fence'])
    expect(calls.data).toBe(0)
  })

  it('重复/乱序 replay_start → 协议失败', async () => {
    const { ws, calls } = await openedWs()
    ws.serverJson({ type: 'replay_start', revision: 2, generation: 4, cursor: 9 })
    ws.serverJson({ type: 'replay_start', revision: 2, generation: 4, cursor: 9 })
    expect(calls.protocol).toEqual(['out_of_order_replay_start'])
  })

  it('replay_start 前的二进制帧 → 协议失败', async () => {
    const { ws, calls } = await openedWs()
    ws.serverBytes('提前输出')
    expect(calls.protocol).toEqual(['unexpected_binary_before_replay_start'])
    expect(calls.data).toBe(0)
  })

  it('非法 JSON / 非对象帧 → 协议失败且之后一切帧被忽略', async () => {
    const { ws, calls } = await openedWs()
    ws.serverRaw('not-json')
    expect(calls.protocol).toEqual(['invalid_json_frame'])
    ws.serverJson({ type: 'replay_start', revision: 2, generation: 4, cursor: 9 })
    ws.serverJson({ type: 'replay_complete', revision: 2, generation: 4, cursor: 9, truncated: false })
    expect(calls.live).toBe(0) // 协议失败后不再放行任何帧
    expect(calls.protocol).toHaveLength(1)
  })

  it('协议失败后 stdin 永不放行（sendInput/sendResize 不再发帧）', async () => {
    const { stream, ws } = await openedWs()
    ws.serverRaw('[1,2]')
    const before = ws.sent.length
    stream.sendInput('x')
    stream.sendResize(80, 24)
    expect(ws.sent).toHaveLength(before)
  })
})

// ---------- P1-3：HTTP decoder 嵌套负例 ----------

describe('HTTP DTO exact decoder（P1-3）', () => {
  const good = ticketView()
  const mutate = (fn: (v: Record<string, unknown>) => void) => {
    const v = JSON.parse(JSON.stringify(good))
    fn(v)
    return v
  }

  it('receipt_refs：null/错型/extra key 逐项 fail-closed', () => {
    expect(() => assertTerminalTicketView(mutate((v) => ((v.ticket as Record<string, unknown>).receipt_refs = [null])))).toThrow(/receipt_refs/)
    expect(() =>
      assertTerminalTicketView(mutate((v) => ((v.ticket as Record<string, unknown>).receipt_refs = [{ type: 'operation', id: 'x', extra: 1 }]))),
    ).toThrow(/键集/)
    expect(() =>
      assertTerminalTicketView(mutate((v) => ((v.ticket as Record<string, unknown>).receipt_refs = [{ type: 1, id: {} }]))),
    ).toThrow(/receipt_refs/)
  })

  it('desired/observed state 枚举闭集', () => {
    expect(() => assertTerminalTicketView(mutate((v) => ((v.ticket as Record<string, unknown>).desired_state = 'flying')))).toThrow(/枚举/)
    expect(() => assertTerminalTicketView(mutate((v) => ((v.ticket as Record<string, unknown>).observed_state = '')))).toThrow(/observed_state/)
  })

  it('G3 顶层 exact {data,meta} 且 meta 必须为对象', async () => {
    const respond = (payload: unknown) => {
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 201, json: async () => payload }) as Response))
    }
    respond({ data: ticketView(), meta: metaOk, extra: 1 })
    await expect(createTerminalTicket(REG_P1, 'w1', 1, 80, 24, 'k1')).rejects.toThrow(/顶层键集/)
    respond({ data: ticketView(), meta: 42 })
    await expect(createTerminalTicket(REG_P1, 'w1', 1, 80, 24, 'k2')).rejects.toThrow(/meta 必须是对象/)
    respond({ data: ticketView(), meta: metaOk })
    await expect(createTerminalTicket(REG_P1, 'w1', 1, 80, 24, 'k3')).resolves.toBeTruthy()
  })
})

// ---------- 页面：fail-closed 外壳 ----------

describe('TerminalPage fail-closed（server 未开启 terminal.pty）', () => {
  it('保持只读外壳，零 POST 零 WS，控制按钮 aria-disabled', async () => {
    const fetchFn = stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1/terminal')
    await waitFor(() => {
      expect(container.querySelector('[data-state="disconnected"]')).toBeInTheDocument()
    })
    for (const name of ['中断', '重连', '重启']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
    }
    expect(container.querySelector('[data-testid="terminal-surface"]')).toBeInTheDocument()
    expect(fetchFn.mock.calls.filter((c) => String(c[0]).includes('terminal-tickets'))).toHaveLength(0)
    expect(FakeWebSocket.instances).toHaveLength(0)
  })
})

// ---------- 页面：live 多 tab 流程 ----------

describe('TerminalPage live（server 开启 terminal.pty）', () => {
  it('空态 → 新终端 → create 精确形状 → attach → replay 后 live；replay 完成前 stdin 不发帧', async () => {
    const writeSpy = vi.spyOn(Terminal.prototype, 'write')
    // xterm onData 是原型 getter（委托实例 core）：用 accessor patch 捕获输入回调
    const onDataDesc = Object.getOwnPropertyDescriptor(Terminal.prototype, 'onData')!
    const onDataGet = onDataDesc.get!
    let inputCb: ((value: string) => void) | null = null
    Object.defineProperty(Terminal.prototype, 'onData', {
      configurable: true,
      get(this: Terminal) {
        const evt = onDataGet.call(this) as (cb: (v: string) => void) => { dispose: () => void }
        return (cb: (v: string) => void) => {
          inputCb = cb
          return evt(cb)
        }
      },
    })
    try {
      const { calls, container } = await renderLive({
        list: EMPTY_LIST,
        create: { status: 201, body: { data: ticketView(), meta: metaOk } },
      })
      const createBtn = (await screen.findAllByRole('button', { name: '新终端' }))[0]
      createBtn.click()
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
      const createCall = calls.find((c) => c.method === 'POST')!
      expect(createCall.url).toBe(`/api/projects/${REG_P1}/workspaces/w1/terminal-tickets`)
      expect(createCall.body).toEqual({ revision: workspaceW1.version, cols: expect.any(Number), rows: expect.any(Number) })
      expect(Object.keys(createCall.body!).sort()).toEqual(['cols', 'revision', 'rows'])
      expect(createCall.headers['Idempotency-Key']).toBeTruthy()

      const ws = FakeWebSocket.instances[0]
      await waitFor(() => expect(ws.sent).toHaveLength(1))
      expect(ws.lastFrame()).toEqual({ type: 'attach', revision: 1, generation: 1, cursor: 0 })

      // replay 完成前：stdin 被门控，任何输入不产生 WS 帧
      await waitFor(() => expect(inputCb).not.toBeNull())
      inputCb!('过早输入')
      expect(ws.sent).toHaveLength(1)

      act(() => {
        ws.serverJson({ type: 'replay_start', revision: 1, generation: 1, cursor: 0 })
        ws.serverBytes('$ echo hi\n')
        ws.serverJson({ type: 'replay_complete', revision: 1, generation: 1, cursor: 0, truncated: false })
      })
      await waitFor(() => expect(writeSpy).toHaveBeenCalled())

      // live 后 stdin 放行
      inputCb!('pwd\n')
      expect(ws.lastFrame()).toEqual({ type: 'input', revision: 1, generation: 1, cursor: 0, input: 'pwd\n' })

      // 稳定 test id
      expect(container.querySelector('[data-testid="terminal-tabs"]')).toBeInTheDocument()
      expect(container.querySelector(`[data-testid="terminal-tab-${TICKET_ID}"]`)).toBeInTheDocument()
      expect(container.querySelector(`[data-testid="terminal-surface-${TICKET_ID}"]`)).toBeInTheDocument()
      expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('状态：运行中')
    } finally {
      Object.defineProperty(Terminal.prototype, 'onData', onDataDesc)
      writeSpy.mockRestore()
    }
  })

  it('已有 running ticket → 自动接管 attach；多 ticket 生成多 tab', async () => {
    const second = ticketView({}, { ticket_id: TICKET_ID_2 })
    await renderLive({
      list: { body: { data: { items: [ticketView(), second], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const tabs = screen.getByTestId('terminal-tabs')
    expect(tabs.querySelector(`[data-testid="terminal-tab-${TICKET_ID}"]`)).toBeInTheDocument()
    expect(tabs.querySelector(`[data-testid="terminal-tab-${TICKET_ID_2}"]`)).toBeInTheDocument()
    // 第二个 ticket 未打开：detached、无 WS
    expect(tabs.querySelector(`[data-testid="terminal-tab-${TICKET_ID_2}"]`)).toHaveClass('terminal-tab--detached')
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('中断 POST 精确形状 + 幂等键', async () => {
    const { calls } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      control: (action) =>
        action === 'interrupt' ? { body: { data: ticketView({}, { revision: 2 }), meta: metaOk } } : undefined,
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    await driveToLive(FakeWebSocket.instances[0])
    const interruptBtn = await screen.findByRole('button', { name: '中断' })
    await waitFor(() => expect(interruptBtn).not.toHaveAttribute('aria-disabled'))
    interruptBtn.click()
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/interrupt'))).toBe(true))
    const call = calls.find((c) => c.url.endsWith('/interrupt'))!
    expect(call.body).toEqual({ revision: 1, generation: 1 })
    expect(call.headers['Idempotency-Key']).toBeTruthy()
  })

  it('关闭标签页：只断开本页 WS，零 POST，不杀 PTY；tab 转 detached', async () => {
    const { calls, container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const ws = FakeWebSocket.instances[0]
    await driveToLive(ws)
    await screen.findByRole('button', { name: '中断' })
    const before = calls.length
    screen.getByRole('button', { name: '关闭标签页' }).click()
    // 零 POST：没有任何控制请求发出
    expect(calls.length).toBe(before)
    expect(calls.some((c) => c.url.includes('/close'))).toBe(false)
    // WS 已断开；tab 保留为 detached（ticket 仍在，可重新接管，PTY 未杀）
    await waitFor(() => expect(ws.closedWith).not.toBeNull())
    await waitFor(() =>
      expect(container.querySelector(`[data-testid="terminal-tab-${TICKET_ID}"]`)).toHaveClass('terminal-tab--detached'),
    )
  })

  it('关闭会话：首次点击仅确认（零 POST），确认后恰好一次 fenced POST /close', async () => {
    const { calls } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      control: (action) =>
        action === 'close'
          ? {
              body: {
                data: ticketView(
                  { state: 'stopped', replay_available: false },
                  { revision: 2, desired_state: 'stopped', observed_state: 'stopped' },
                ),
                meta: metaOk,
              },
            }
          : undefined,
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    await driveToLive(FakeWebSocket.instances[0])
    const closeBtn = await screen.findByRole('button', { name: '关闭会话' })
    closeBtn.click()
    // 第一次点击只弹确认，不发 POST
    await screen.findByText('确认关闭会话？')
    expect(calls.filter((c) => c.url.includes('/close'))).toHaveLength(0)
    // 确认：恰好一次 POST /close
    const confirmBtn = screen.getAllByRole('button', { name: '关闭会话' }).pop()!
    confirmBtn.click()
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/close'))).toHaveLength(1))
    const call = calls.find((c) => c.url.endsWith('/close'))!
    expect(call.body).toEqual({ revision: 1, generation: 1 })
    expect(call.headers['Idempotency-Key']).toBeTruthy()
    await screen.findByText('终端会话已停止')
  })

  it('WS 4409 → 先 refetch 权威 projection → reconnecting；重连 POST 精确形状 + 新 fence attach', async () => {
    const { calls } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      detail: () => ({ body: { data: ticketView({}, { revision: 1 }), meta: metaOk } }),
      control: (action) =>
        action === 'reconnect'
          ? { body: { data: ticketView({}, { revision: 2, reconnect_cursor: 1 }), meta: metaOk } }
          : undefined,
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const ws1 = FakeWebSocket.instances[0]
    await driveToLive(ws1)
    await screen.findByRole('button', { name: '中断' })
    act(() => {
      ws1.serverClose(4409, 'stale')
    })
    await screen.findByText('终端连接已断开')
    // 先 refetch（GET detail），不盲重连
    await waitFor(() => expect(calls.some((c) => c.method === 'GET' && c.url.endsWith(`/terminal-tickets/${TICKET_ID}`))).toBe(true))
    expect(calls.some((c) => c.url.endsWith('/reconnect'))).toBe(false)
    screen.getByRole('button', { name: '重连' }).click()
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/reconnect'))).toBe(true))
    const call = calls.find((c) => c.url.endsWith('/reconnect'))!
    expect(Object.keys(call.body!).sort()).toEqual(['cols', 'cursor', 'generation', 'revision', 'rows'])
    expect(call.body!.revision).toBe(1)
    expect(call.body!.cursor).toBe(0)
    expect(call.headers['Idempotency-Key']).toBeUndefined()
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))
    const ws2 = FakeWebSocket.instances[1]
    await waitFor(() => expect(ws2.sent).toHaveLength(1))
    expect(ws2.lastFrame()).toEqual({ type: 'attach', revision: 2, generation: 1, cursor: 1 })
  })

  it('exit 帧 → exited 态；runtime-state 行可见', async () => {
    const { container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const ws = FakeWebSocket.instances[0]
    await driveToLive(ws)
    act(() => {
      ws.serverJson({ type: 'exit', generation: 1 })
    })
    await screen.findByText('终端进程已退出')
    expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('连接：已退出')
  })

  it('process_unknown → degraded 恢复提示；中断禁用、重启可用', async () => {
    await renderLive({
      list: {
        body: {
          data: { items: [ticketView({ state: 'process_unknown', replay_available: false })], next_cursor: null },
          meta: metaOk,
        },
      },
    })
    await screen.findByText('终端进程状态未知')
    expect(screen.getByRole('button', { name: '中断' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: '重启' })).not.toHaveAttribute('aria-disabled')
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it('复制标识：只含公开 project/workspace/ticket ID', async () => {
    const written: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: (t: string) => (written.push(t), Promise.resolve()) },
      configurable: true,
    })
    await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const copyBtn = await screen.findByRole('button', { name: '复制标识' })
    copyBtn.click()
    await waitFor(() => expect(written).toHaveLength(1))
    expect(written[0]).toBe(`project=${REG_P1} workspace=w1 ticket=${TICKET_ID}`)
    for (const banned of ['cwd', 'path', 'pid', 'herdr', 'pane']) {
      expect(written[0]).not.toContain(banned)
    }
  })

  it('全屏切换：terminal-fullscreen 类开合', async () => {
    const { container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const fsBtn = await screen.findByRole('button', { name: '全屏' })
    await waitFor(() => expect(fsBtn).not.toHaveAttribute('aria-disabled'))
    fsBtn.click()
    await waitFor(() => expect(container.querySelector('.terminal-fullscreen')).toBeInTheDocument())
    fsBtn.click()
    await waitFor(() => expect(container.querySelector('.terminal-fullscreen')).not.toBeInTheDocument())
  })

  it('malformed/未知帧 fail-closed：协议失败 → error 态，stdin/control 关闭，后续帧零作用', async () => {
    const onDataDesc = Object.getOwnPropertyDescriptor(Terminal.prototype, 'onData')!
    const onDataGet = onDataDesc.get!
    let inputCb: ((value: string) => void) | null = null
    Object.defineProperty(Terminal.prototype, 'onData', {
      configurable: true,
      get(this: Terminal) {
        const evt = onDataGet.call(this) as (cb: (v: string) => void) => { dispose: () => void }
        return (cb: (v: string) => void) => {
          inputCb = cb
          return evt(cb)
        }
      },
    })
    const writeSpy = vi.spyOn(Terminal.prototype, 'write')
    try {
      const { container } = await renderLive({
        list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      })
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
      const ws = FakeWebSocket.instances[0]
      await driveToLive(ws)
      await waitFor(() => expect(inputCb).not.toBeNull())
      const interruptBtn = await screen.findByRole('button', { name: '中断' })
      await waitFor(() => expect(interruptBtn).not.toHaveAttribute('aria-disabled'))

      // live 中到达非法帧：协议失败（act 包裹，等待 React state flush）
      act(() => {
        ws.serverRaw('not-json-at-all')
      })
      await screen.findByText(/连接被服务端拒绝/)
      await waitFor(() =>
        expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('连接：错误'),
      )
      // stdin 关闭：输入不再产生 WS 帧
      const sentBefore = ws.sent.length
      act(() => {
        inputCb!('不应发出')
      })
      expect(ws.sent).toHaveLength(sentBefore)
      // control 关闭
      expect(screen.getByRole('button', { name: '中断' })).toHaveAttribute('aria-disabled', 'true')
      // 后续帧（合法形态亦同）零作用：状态滞留 error、无新写入、无新连接
      const writesBefore = writeSpy.mock.calls.length
      act(() => {
        ws.serverJson({ type: 'replay_start', revision: 1, generation: 1, cursor: 0 })
        ws.serverBytes('迟到的输出')
        ws.serverJson({ type: 'exit', generation: 1 })
        ws.serverRaw('{"type":"mystery"}')
      })
      expect(writeSpy.mock.calls.length).toBe(writesBefore)
      expect(screen.queryByText('终端进程已退出')).not.toBeInTheDocument()
      expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('连接：错误')
      expect(FakeWebSocket.instances).toHaveLength(1)
    } finally {
      Object.defineProperty(Terminal.prototype, 'onData', onDataDesc)
      writeSpy.mockRestore()
    }
  })

  it('interrupt 推进 revision 后：旧 WS fence 输入被门控，新 attach 用新 fence', async () => {
    const onDataDesc = Object.getOwnPropertyDescriptor(Terminal.prototype, 'onData')!
    const onDataGet = onDataDesc.get!
    let inputCb: ((value: string) => void) | null = null
    Object.defineProperty(Terminal.prototype, 'onData', {
      configurable: true,
      get(this: Terminal) {
        const evt = onDataGet.call(this) as (cb: (v: string) => void) => { dispose: () => void }
        return (cb: (v: string) => void) => {
          inputCb = cb
          return evt(cb)
        }
      },
    })
    try {
      const { calls } = await renderLive({
        list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
        control: (action) =>
          action === 'interrupt' ? { body: { data: ticketView({}, { revision: 2 }), meta: metaOk } } : undefined,
      })
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
      const ws1 = FakeWebSocket.instances[0]
      await driveToLive(ws1)
      await waitFor(() => expect(inputCb).not.toBeNull())
      const interruptBtn = await screen.findByRole('button', { name: '中断' })
      await waitFor(() => expect(interruptBtn).not.toHaveAttribute('aria-disabled'))
      interruptBtn.click()
      await waitFor(() => expect(calls.some((c) => c.url.endsWith('/interrupt'))).toBe(true))
      // interrupt 成功后页面用新 fence 重挂
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))
      const ws2 = FakeWebSocket.instances[1]
      await waitFor(() => expect(ws2.sent).toHaveLength(1))
      expect(ws2.lastFrame()).toEqual({ type: 'attach', revision: 2, generation: 1, cursor: 0 })
      // 旧连接不再接收任何输入帧
      const ws1Sent = ws1.sent.length
      act(() => {
        ws2.serverJson({ type: 'replay_start', revision: 2, generation: 1, cursor: 0 })
        ws2.serverJson({ type: 'replay_complete', revision: 2, generation: 1, cursor: 0, truncated: false })
      })
      inputCb!('date\n')
      expect(ws1.sent).toHaveLength(ws1Sent)
      expect(ws2.lastFrame()).toEqual({ type: 'input', revision: 2, generation: 1, cursor: 0, input: 'date\n' })
    } finally {
      Object.defineProperty(Terminal.prototype, 'onData', onDataDesc)
    }
  })

  it('P1-1 页面级：伪 replay_complete 提前到达 → 协议错误态且 stdin 保持关闭', async () => {
    const onDataDesc = Object.getOwnPropertyDescriptor(Terminal.prototype, 'onData')!
    const onDataGet = onDataDesc.get!
    let inputCb: ((value: string) => void) | null = null
    Object.defineProperty(Terminal.prototype, 'onData', {
      configurable: true,
      get(this: Terminal) {
        const evt = onDataGet.call(this) as (cb: (v: string) => void) => { dispose: () => void }
        return (cb: (v: string) => void) => {
          inputCb = cb
          return evt(cb)
        }
      },
    })
    try {
      await renderLive({
        list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      })
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
      const ws = FakeWebSocket.instances[0]
      await waitFor(() => expect(ws.sent).toHaveLength(1))
      // 未 replay_start 直接 replay_complete：协议违反
      act(() => {
        ws.serverJson({ type: 'replay_complete', revision: 1, generation: 1, cursor: 0, truncated: false })
      })
      await screen.findByText(/连接被服务端拒绝/)
      // stdin 保持关闭：输入不产生任何 WS 帧
      await waitFor(() => expect(inputCb).not.toBeNull())
      const before = ws.sent.length
      inputCb!('不应发出')
      expect(ws.sent).toHaveLength(before)
      // 中断等控制不开放
      expect(screen.getByRole('button', { name: '中断' })).toHaveAttribute('aria-disabled', 'true')
    } finally {
      Object.defineProperty(Terminal.prototype, 'onData', onDataDesc)
    }
  })

  it('P1-2：interrupt 换新 generation 后，旧 stream 的 replay/data/exit/error 零污染', async () => {
    const writeSpy = vi.spyOn(Terminal.prototype, 'write')
    const { calls, container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      control: (action) =>
        action === 'interrupt' ? { body: { data: ticketView({}, { revision: 2 }), meta: metaOk } } : undefined,
    })
    try {
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
      const ws1 = FakeWebSocket.instances[0]
      await driveToLive(ws1)
      const interruptBtn = await screen.findByRole('button', { name: '中断' })
      await waitFor(() => expect(interruptBtn).not.toHaveAttribute('aria-disabled'))
      interruptBtn.click()
      await waitFor(() => expect(calls.some((c) => c.url.endsWith('/interrupt'))).toBe(true))
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))
      const ws2 = FakeWebSocket.instances[1]
      await waitFor(() => expect(ws2.sent).toHaveLength(1))
      // 旧连接排队回调全部到达：不得改写新 generation 状态
      const writesBefore = writeSpy.mock.calls.length
      ws1.serverJson({ type: 'replay_start', revision: 1, generation: 1, cursor: 0 })
      ws1.serverBytes('旧连接的迟到输出')
      ws1.serverJson({ type: 'replay_complete', revision: 1, generation: 1, cursor: 0, truncated: false })
      ws1.serverJson({ type: 'exit', generation: 1 })
      ws1.serverJson({ type: 'error', code: 'terminal_io_unavailable' })
      ws1.serverClose(4409, 'taken over')
      // 零变化：无新写入、phase 仍是新连接的 attaching、无 exited/error 文案
      expect(writeSpy.mock.calls.length).toBe(writesBefore)
      expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('连接：连接中')
      expect(screen.queryByText('终端进程已退出')).not.toBeInTheDocument()
      expect(screen.queryByText(/终端流错误/)).not.toBeInTheDocument()
      expect(screen.queryByText('终端连接已断开')).not.toBeInTheDocument()
      // 新连接正常完成 replay → live
      act(() => {
        ws2.serverJson({ type: 'replay_start', revision: 2, generation: 1, cursor: 0 })
        ws2.serverJson({ type: 'replay_complete', revision: 2, generation: 1, cursor: 0, truncated: false })
      })
      await waitFor(() =>
        expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('连接：已连接'),
      )
    } finally {
      writeSpy.mockRestore()
    }
  })

  it('P1-4：create 首次失败 → viewport 变化 → 重试复用同一 key + byte-equivalent body', async () => {
    const { calls } = await renderLive({
      list: EMPTY_LIST,
      create: (attempt) =>
        attempt === 1
          ? { status: 503, body: { error: { code: 'terminal_io_unavailable', message: 'io', retryable: true } } }
          : { status: 201, body: { data: ticketView(), meta: metaOk } },
    })
    // 第一次创建：503 失败
    const firstBtn = (await screen.findAllByRole('button', { name: '新终端' }))[0]
    await act(async () => {
      firstBtn.click()
    })
    await screen.findByText('终端不可用')
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1)
    // 重试按钮恢复列表 → 空态
    await act(async () => {
      screen.getByRole('button', { name: '重试' }).click()
    })
    await screen.findByText('还没有终端')
    // 两次点击之间改变 viewport
    act(() => {
      ;(window as { innerWidth: number }).innerWidth = 500
      window.dispatchEvent(new Event('resize'))
    })
    // 第二次创建：成功
    const secondBtn = (await screen.findAllByRole('button', { name: '新终端' }))[0]
    await act(async () => {
      secondBtn.click()
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(posts[1].headers['Idempotency-Key']).toBe(posts[0].headers['Idempotency-Key'])
    expect(JSON.stringify(posts[1].body)).toBe(JSON.stringify(posts[0].body))
  })

  it('P1-4：控制失败保留 phase 与同 intent；同按钮重试复用同一 key/body', async () => {
    let interruptCalls = 0
    const { calls } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
      control: (action) => {
        if (action !== 'interrupt') return undefined
        interruptCalls += 1
        if (interruptCalls === 1) {
          return { status: 503, body: { error: { code: 'terminal_io_unavailable', message: 'io', retryable: true } } }
        }
        return { body: { data: ticketView({}, { revision: 2 }), meta: metaOk } }
      },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    await driveToLive(FakeWebSocket.instances[0])
    const interruptBtn = await screen.findByRole('button', { name: '中断' })
    await waitFor(() => expect(interruptBtn).not.toHaveAttribute('aria-disabled'))
    interruptBtn.click()
    // 失败：degraded 提示但保持 live（不被踢到 error 态）
    await screen.findByText('终端操作未完成')
    expect(interruptBtn).not.toHaveAttribute('aria-disabled')
    // viewport/fence 环境变化后同按钮重试
    window.dispatchEvent(new Event('resize'))
    interruptBtn.click()
    await waitFor(() => expect(interruptCalls).toBe(2))
    const posts = calls.filter((c) => c.url.endsWith('/interrupt'))
    expect(posts).toHaveLength(2)
    expect(posts[0].headers['Idempotency-Key']).toBe(posts[1].headers['Idempotency-Key'])
    expect(JSON.stringify(posts[0].body)).toBe(JSON.stringify(posts[1].body))
  })

  it('P1-5：fullscreen overlay 内可点「退出全屏」按钮与 Escape；390 视口下退出条仍在', async () => {
    const { container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const fsBtn = await screen.findByRole('button', { name: '全屏' })
    await waitFor(() => expect(fsBtn).not.toHaveAttribute('aria-disabled'))
    // 390 视口
    ;(window as { innerWidth: number }).innerWidth = 390
    window.dispatchEvent(new Event('resize'))
    fsBtn.click()
    const overlay = await waitFor(() => {
      const el = container.querySelector('.terminal-fullscreen')
      expect(el).toBeInTheDocument()
      return el!
    })
    // overlay 内真实退出按钮（不是依赖被覆盖的 PageHeader 按钮）
    const exitBtn = within(overlay as HTMLElement).getByRole('button', { name: '退出全屏' })
    exitBtn.click()
    await waitFor(() => expect(container.querySelector('.terminal-fullscreen')).not.toBeInTheDocument())
    // Escape 路径
    screen.getByRole('button', { name: '全屏' }).click()
    await waitFor(() => expect(container.querySelector('.terminal-fullscreen')).toBeInTheDocument())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(container.querySelector('.terminal-fullscreen')).not.toBeInTheDocument())
    // 390 下 runtime 长行带 overflow 保护类（布局断言在 jsdom 无排版，锚定结构类）
    const stateLine = container.querySelector('[data-testid="terminal-runtime-state"]')
    expect(stateLine).toHaveClass('terminal-runtime-state')
  })

  it('列表 503 → error 态 + 重试；create 409 → error 态且零 WS', async () => {
    await renderLive({
      list: { status: 503, body: { error: { code: 'terminal_io_unavailable', message: '终端 I/O 不可用', retryable: true } } },
    })
    await screen.findByText('终端不可用')
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('create 409 revision_conflict → error 态且零 WS', async () => {
    await renderLive({
      list: EMPTY_LIST,
      create: { status: 409, body: { error: { code: 'revision_conflict', message: 'revision 冲突', retryable: false } } },
    })
    const createBtn = (await screen.findAllByRole('button', { name: '新终端' }))[0]
    createBtn.click()
    await screen.findByText('终端不可用')
    expect(FakeWebSocket.instances).toHaveLength(0)
  })
})

// ---------- 首用可理解性：可见文案无内部术语 ----------
describe('TerminalPage 首用可理解性（用户语言）', () => {
  const BANNED = ['PTY', 'ticket', 'generation', 'revision', 'authority', 'identity', 'Workspace', 'POST']

  it('空态：标题/描述/按钮无内部术语', async () => {
    await renderLive({ list: EMPTY_LIST })
    await screen.findByText('还没有终端')
    const text = document.querySelector('main')?.textContent ?? ''
    for (const banned of BANNED) expect(text).not.toContain(banned)
  })

  it('live happy path：tab 用「终端 N」人类标签，页面无内部术语', async () => {
    await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    await driveToLive(FakeWebSocket.instances[0])
    await screen.findByRole('button', { name: '中断' })
    expect(screen.getByText('终端 1')).toBeInTheDocument()
    const text = document.querySelector('main')?.textContent ?? ''
    for (const banned of BANNED) expect(text).not.toContain(banned)
  })
})
