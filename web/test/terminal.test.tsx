import { screen, waitFor } from '@testing-library/react'
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
  create?: RouteSpec
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
        spec = method === 'POST' ? routes.create : routes.list
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

/** attach 并完成 replay，进入 live */
async function driveToLive(ws: FakeWebSocket, fence = { revision: 1, generation: 1, cursor: 0 }) {
  await waitFor(() => expect(ws.sent).toHaveLength(1))
  expect(ws.lastFrame()).toEqual({ type: 'attach', ...fence })
  ws.serverJson({ type: 'replay_start', ...fence })
  ws.serverJson({ type: 'replay_complete', ...fence, truncated: false })
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

      ws.serverJson({ type: 'replay_start', revision: 1, generation: 1, cursor: 0 })
      ws.serverBytes('$ echo hi\n')
      ws.serverJson({ type: 'replay_complete', revision: 1, generation: 1, cursor: 0, truncated: false })
      await waitFor(() => expect(writeSpy).toHaveBeenCalled())

      // live 后 stdin 放行
      inputCb!('pwd\n')
      expect(ws.lastFrame()).toEqual({ type: 'input', revision: 1, generation: 1, cursor: 0, input: 'pwd\n' })

      // 稳定 test id
      expect(container.querySelector('[data-testid="terminal-tabs"]')).toBeInTheDocument()
      expect(container.querySelector(`[data-testid="terminal-tab-${TICKET_ID}"]`)).toBeInTheDocument()
      expect(container.querySelector(`[data-testid="terminal-surface-${TICKET_ID}"]`)).toBeInTheDocument()
      expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('runtime=running')
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
    ws1.serverClose(4409, 'stale')
    await screen.findByText('终端流已断开')
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
    ws.serverJson({ type: 'exit', generation: 1 })
    await screen.findByText('终端进程已退出')
    expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('流=exited')
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

  it('复制 identity：只含公开 project/workspace/ticket ID', async () => {
    const written: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: (t: string) => (written.push(t), Promise.resolve()) },
      configurable: true,
    })
    await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const copyBtn = await screen.findByRole('button', { name: '复制 identity' })
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

  it('malformed/未知帧 fail-closed：不崩溃、不改态、零副作用', async () => {
    const { container } = await renderLive({
      list: { body: { data: { items: [ticketView()], next_cursor: null }, meta: metaOk } },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const ws = FakeWebSocket.instances[0]
    await driveToLive(ws)
    await screen.findByRole('button', { name: '中断' })
    const sentBefore = ws.sent.length
    ws.serverRaw('not-json-at-all')
    ws.serverRaw('{"type":"mystery"}')
    ws.serverRaw('{"type":"input","input":"注入"}') // server→client 只允许 replay/exit/error
    ws.serverRaw('[1,2]')
    // 仍是 live，无 error banner，无新帧，无新连接
    expect(container.querySelector('[data-testid="terminal-runtime-state"]')?.textContent).toContain('流=live')
    expect(screen.queryByText('终端错误')).not.toBeInTheDocument()
    expect(ws.sent).toHaveLength(sentBefore)
    expect(FakeWebSocket.instances).toHaveLength(1)
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
      ws2.serverJson({ type: 'replay_start', revision: 2, generation: 1, cursor: 0 })
      ws2.serverJson({ type: 'replay_complete', revision: 2, generation: 1, cursor: 0, truncated: false })
      inputCb!('date\n')
      expect(ws1.sent).toHaveLength(ws1Sent)
      expect(ws2.lastFrame()).toEqual({ type: 'input', revision: 2, generation: 1, cursor: 0, input: 'date\n' })
    } finally {
      Object.defineProperty(Terminal.prototype, 'onData', onDataDesc)
    }
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
