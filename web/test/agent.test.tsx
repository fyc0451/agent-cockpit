import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import {
  assertAgentView,
  createAgent,
  getAgent,
  sendAgentPrompt,
  type AgentView,
} from '../api/agents'
import { agentMailStatus, defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'

// ---------- fixtures ----------

// 每个测试等同一个全新浏览器：localStorage（P1-b 最近会话记录）跨用例清零
beforeEach(() => {
  window.localStorage.clear()
})

const AGENT_ID = 'ag_test1'
const AGENTS_BASE = `/api/projects/${REG_P1}/workspaces/w1/agents`
const AGENT_ROUTE = '/projects/p1/workspaces/w1/agent'

function agentView(over: Partial<AgentView> = {}): AgentView {
  return {
    agent_id: AGENT_ID,
    project_id: REG_P1,
    workspace_id: 'w1',
    kind: 'codex',
    status: 'idle',
    transcript: '',
    ...over,
  }
}

/** env-check 变体：一个 Agent CLI 都没装 */
const envNonePayload = {
  herdr: { installed: false, path: '' },
  agents: {
    codex: { installed: false, path: '' },
    kimi: { installed: false, path: '' },
    claude: { installed: false, path: '' },
  },
  agent_mail: agentMailStatus,
}

interface FetchCall {
  url: string
  method: string
  body?: Record<string, unknown>
  headers: Record<string, string>
}

interface Spec {
  status?: number
  body: unknown
}

interface AgentRoutes {
  create?: (attempt: number) => Spec | undefined
  detail?: (call: number) => Spec | undefined
  prompt?: (attempt: number) => Spec | undefined
  envCheck?: unknown
}

/** Agent 页测试世界：agents 路由按 method 分派并记录调用；其余走默认 map */
function renderAgentWorld(routes: AgentRoutes, initialRoute = AGENT_ROUTE) {
  const calls: FetchCall[] = []
  const counters = { create: 0, detail: 0, prompt: 0 }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    let spec: Spec | undefined
    if (url === AGENTS_BASE || url.startsWith(`${AGENTS_BASE}/`)) {
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      })
      if (url === AGENTS_BASE && method === 'POST') {
        counters.create += 1
        spec = routes.create?.(counters.create)
      } else if (url.endsWith('/prompts')) {
        counters.prompt += 1
        spec = routes.prompt?.(counters.prompt)
      } else {
        counters.detail += 1
        spec = routes.detail?.(counters.detail)
      }
    } else {
      const map: Record<string, unknown> = { ...defaultFetchMap() }
      if (routes.envCheck) map['/api/env-check'] = routes.envCheck
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
  const rendered = renderApp(initialRoute)
  return { ...rendered, calls, counters }
}

// ---------- 单元：agents API 守卫与请求形状 ----------

describe('agents API 守卫与请求形状', () => {
  it('agent view 精确六键 fail-closed（缺键/多键都拒）；status 收紧为 idle|working|blocked|done|unknown', () => {
    const good = agentView()
    expect(assertAgentView(JSON.parse(JSON.stringify(good)))).toBeTruthy()
    const missing = JSON.parse(JSON.stringify(good)) as Record<string, unknown>
    delete missing.transcript
    expect(() => assertAgentView(missing)).toThrow(/键集/)
    const extra = JSON.parse(JSON.stringify(good))
    extra.pane_id = '%1'
    expect(() => assertAgentView(extra)).toThrow(/键集/)
    // status 闭集：五个合法值放行，其他（含旧 error/虚构值）拒绝
    for (const ok of ['idle', 'working', 'blocked', 'done', 'unknown']) {
      const v = JSON.parse(JSON.stringify(good))
      v.status = ok
      expect(assertAgentView(v)).toBeTruthy()
    }
    for (const bad of ['error', 'flying', '']) {
      const v = JSON.parse(JSON.stringify(good))
      v.status = bad
      expect(() => assertAgentView(v)).toThrow(/status/)
    }
  })

  it('create body 精确 {kind} + Idempotency-Key；不含 workdir/command/session/pane', async () => {
    const captured: FetchCall[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      captured.push({
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      })
      return { ok: true, status: 201, json: async () => ({ data: agentView(), meta: metaOk }) } as Response
    }))
    await createAgent(REG_P1, 'w1', 'codex', 'idem-1')
    expect(captured).toHaveLength(1)
    const call = captured[0]
    expect(call.method).toBe('POST')
    expect(call.url).toBe(AGENTS_BASE)
    expect(call.body).toEqual({ kind: 'codex' })
    expect(call.headers['Idempotency-Key']).toBe('idem-1')
    const raw = JSON.stringify(call.body)
    for (const banned of ['workdir', 'cwd', 'command', 'argv', 'env', 'pid', 'session', 'pane', 'herdr']) {
      expect(raw).not.toContain(banned)
    }
  })

  it('prompt body 精确 {prompt} + Idempotency-Key；GET detail 无 body', async () => {
    const captured: FetchCall[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      captured.push({
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      })
      return { ok: true, status: 200, json: async () => ({ data: agentView(), meta: metaOk }) } as Response
    }))
    await sendAgentPrompt({ projectId: REG_P1, workspaceId: 'w1', agentId: AGENT_ID }, '做点什么', 'idem-2')
    const promptCall = captured[0]
    expect(promptCall.method).toBe('POST')
    expect(promptCall.url).toBe(`${AGENTS_BASE}/${AGENT_ID}/prompts`)
    expect(promptCall.body).toEqual({ prompt: '做点什么' })
    expect(promptCall.headers['Idempotency-Key']).toBe('idem-2')

    const view = await getAgent({ projectId: REG_P1, workspaceId: 'w1', agentId: AGENT_ID })
    expect(view.agent_id).toBe(AGENT_ID)
    const getCall = captured[1]
    expect(getCall.method).toBe('GET')
    expect(getCall.url).toBe(`${AGENTS_BASE}/${AGENT_ID}`)
    expect(getCall.body).toBeUndefined()
  })
})

// ---------- 页面：Agent 工作页 ----------

describe('AgentPage', () => {
  it('Agent 类型只列出 env-check 已安装的 CLI，默认选中第一个', async () => {
    await renderAgentWorld({})
    const select = (await screen.findByLabelText('Agent 类型')) as HTMLSelectElement
    const options = within(select).getAllByRole('option').map((o) => o.textContent)
    // 默认 fixture：codex/kimi 已装，claude/qodercli/grok/opencode 未装
    expect(options).toEqual(['codex', 'kimi'])
    expect(select.value).toBe('codex')
  })

  it('一个 CLI 都没装：明确提示先安装，不出表单、零 POST', async () => {
    const { calls } = await renderAgentWorld({ envCheck: envNonePayload })
    await screen.findByText('还没有可用的 Agent')
    expect(screen.getByText(/先安装一个受支持的/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始任务' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('任务')).not.toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('第一次一键开始：自动 create（精确 {kind}）后发送 prompt；状态行可见', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({ body: { data: agentView({ status: 'working' }), meta: metaOk } }),
      detail: () => ({ body: { data: agentView({ status: 'working' }), meta: metaOk } }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '修复登录回归' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === AGENTS_BASE)).toBe(true))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/prompts'))).toBe(true))
    const create = calls.find((c) => c.url === AGENTS_BASE)!
    expect(create.body).toEqual({ kind: 'codex' })
    expect(create.headers['Idempotency-Key']).toBeTruthy()
    const prompt = calls.find((c) => c.url.endsWith('/prompts'))!
    expect(prompt.body).toEqual({ prompt: '修复登录回归' })
    expect(prompt.headers['Idempotency-Key']).toBeTruthy()
    await waitFor(() =>
      expect(screen.getByTestId('agent-status').textContent).toContain('正在执行'),
    )
  })

  it('刷新恢复：URL 带 agent 参数 → GET 恢复 transcript，零 POST', async () => {
    const { calls } = await renderAgentWorld(
      {
        detail: () => ({
          body: { data: agentView({ transcript: '第一轮回复内容' }), meta: metaOk },
        }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('第一轮回复内容')
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
    expect(screen.getByTestId('agent-status').textContent).toContain('状态：')
  })

  it('已有 agent 时继续发送第二条：只发 prompts，不再 create', async () => {
    const { calls } = await renderAgentWorld(
      {
        detail: () => ({
          body: { data: agentView({ transcript: '第一轮回复内容' }), meta: metaOk },
        }),
        prompt: () => ({
          body: { data: agentView({ status: 'working', transcript: '第一轮回复内容' }), meta: metaOk },
        }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('第一轮回复内容')
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '再加一条边界用例' } })
    screen.getByRole('button', { name: '发送' }).click()
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(1))
    expect(calls.some((c) => c.method === 'POST' && c.url === AGENTS_BASE)).toBe(false)
  })

  it('空任务：主按钮禁用且零 POST', async () => {
    const { calls } = await renderAgentWorld({})
    const btn = await screen.findByRole('button', { name: '开始任务' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    btn.click()
    expect(calls).toHaveLength(0)
  })

  it('start 503（workspace_agent_unavailable）同 key 第二次成功：重试复用同一 Idempotency-Key 与 byte-equivalent body', async () => {
    const { calls } = await renderAgentWorld({
      create: (attempt) =>
        attempt === 1
          ? { status: 503, body: { error: { code: 'workspace_agent_unavailable', message: 'workspace agent unavailable', retryable: true } } }
          : { status: 201, body: { data: agentView(), meta: metaOk } },
      prompt: () => ({ body: { data: agentView(), meta: metaOk } }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务A' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText(/这台工作空间暂时无法启动 Agent/)
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'POST' && c.url === AGENTS_BASE)).toHaveLength(2),
    )
    const posts = calls.filter((c) => c.url === AGENTS_BASE)
    expect(posts[1].headers['Idempotency-Key']).toBe(posts[0].headers['Idempotency-Key'])
    expect(JSON.stringify(posts[1].body)).toBe(JSON.stringify(posts[0].body))
  })

  it('首个响应 idle 也进入有界刷新窗口：持续 GET 直到 transcript 变化后停止', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      // 关键竞态：prompt 响应仍是 idle 且 transcript 未变
      prompt: () => ({ body: { data: agentView({ status: 'idle', transcript: '' }), meta: metaOk } }),
      detail: (n) => ({
        body: {
          data: agentView(n < 2 ? { status: 'idle', transcript: '' } : { status: 'idle', transcript: '第一条回复' }),
          meta: metaOk,
        },
      }),
    })
    const gets = () => calls.filter((c) => c.method === 'GET').length
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务B' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/prompts'))).toBe(true))
    // idle 首响应不丢回复：轮询窗口启动
    await waitFor(() => expect(gets()).toBeGreaterThanOrEqual(1), { timeout: 2500 })
    await screen.findByText('第一条回复', undefined, { timeout: 4000 })
    // transcript 变化后窗口关闭：GET 不再增长
    const settled = gets()
    await new Promise((r) => setTimeout(r, 2200))
    expect(gets()).toBe(settled)
  }, 15000)

  it('轮询可清理：卸载后不再有 GET', async () => {
    const { calls, unmount } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({ body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk } }),
      detail: () => ({ body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk } }),
    })
    const gets = () => calls.filter((c) => c.method === 'GET').length
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务C' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() => expect(gets()).toBeGreaterThanOrEqual(1), { timeout: 2500 })
    unmount()
    const at = gets()
    await new Promise((r) => setTimeout(r, 2200))
    expect(gets()).toBe(at)
  }, 15000)

  it('入口：工作区首页突出「开始任务」，Rail 工作区段有 Agent 链接', async () => {
    await renderAgentWorld({}, '/projects/p1/workspaces/w1')
    const cta = await screen.findByRole('link', { name: /开始任务/ })
    expect(cta).toHaveAttribute('href', AGENT_ROUTE)
    const rail = screen.getByRole('navigation', { name: '主导航' })
    const agentLink = await within(rail).findByTitle('Agent')
    expect(agentLink).toHaveAttribute('href', AGENT_ROUTE)
  })

  it('390 窄屏：主按钮/输入/Rail Agent 入口可发现（结构断言，同 P1-5 先例）', async () => {
    ;(window as { innerWidth: number }).innerWidth = 390
    window.dispatchEvent(new Event('resize'))
    await renderAgentWorld({})
    expect(await screen.findByLabelText('任务')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始任务' })).toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: '主导航' })
    const agentItem = await within(rail).findByTitle('Agent')
    expect(agentItem).toHaveClass('rail-item--mobile-core')
    // workspace 上下文下移动端底栏让位：global 三项 mobile-hidden，只留 项目/文件/终端/Agent（桌面不变）
    for (const title of ['需要你处理', '提问与回复', '设置']) {
      expect(within(rail).getByTitle(title)).toHaveClass('rail-item--mobile-hidden')
    }
    for (const title of ['项目', '文件', '终端', 'Agent']) {
      expect(within(rail).getByTitle(title)).not.toHaveClass('rail-item--mobile-hidden')
    }
  })
})

// ---------- 产品验收：新用户首用旅程（闭环） ----------

/**
 * 有状态假后端：create/prompt/detail 共享同一份会话——“刷新”（新 renderApp）
 * 之后 GET 仍能恢复，prompt 的回复在下一次 detail GET 时到达（模拟异步执行）。
 * create 延迟 50ms 以便断言“启动中”瞬态。
 */
function journeyBackend(opts: { failFirstCreate?: boolean } = {}) {
  let stored: AgentView | null = null
  let createAttempts = 0
  let replyPending: string | null = null
  const calls: FetchCall[] = []
  const install = () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      let spec: Spec | undefined
      if (url === AGENTS_BASE || url.startsWith(`${AGENTS_BASE}/`)) {
        calls.push({
          url,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          headers: (init?.headers ?? {}) as Record<string, string>,
        })
        if (url === AGENTS_BASE && method === 'POST') {
          createAttempts += 1
          await new Promise((r) => setTimeout(r, 50))
          if (opts.failFirstCreate && createAttempts === 1) {
            spec = {
              status: 503,
              body: { error: { code: 'workspace_agent_unavailable', message: 'workspace agent unavailable', retryable: true } },
            }
          } else {
            stored = agentView()
            spec = { status: 201, body: { data: stored, meta: metaOk } }
          }
        } else if (url.endsWith('/prompts')) {
          const text = (JSON.parse(String(init!.body)) as { prompt: string }).prompt
          stored = { ...stored!, status: 'working' }
          replyPending = `回复：${text}`
          spec = { body: { data: stored, meta: metaOk } }
        } else if (!stored) {
          spec = { status: 404, body: { error: { code: 'not_found', message: 'no agent', retryable: false } } }
        } else {
          if (replyPending) {
            stored = {
              ...stored,
              status: 'idle',
              transcript: `${stored.transcript}${stored.transcript ? '\n' : ''}${replyPending}`,
            }
            replyPending = null
          }
          spec = { body: { data: stored, meta: metaOk } }
        }
      } else {
        const map = defaultFetchMap()
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
  }
  return { install, calls, creates: () => createAttempts }
}

/** 页面（main）不得出现的内部实现词 */
const JOURNEY_BANNED = ['Herdr', 'session', 'pane', 'cwd', 'PID', 'argv', 'workdir']
const mainText = () => document.querySelector('main')?.textContent ?? ''

describe('AgentPage 首用旅程（产品验收）', () => {
  it('新用户闭环：首页主操作 → 已安装 Agent → 首条任务（含错误重试）→ 处理中 → 回复 → 刷新 → 第二条', async () => {
    const backend = journeyBackend({ failFirstCreate: true })
    backend.install()

    // 1. Workspace 首页：无需文档即可见明确主操作
    renderApp('/projects/p1/workspaces/w1')
    const cta = await screen.findByRole('link', { name: /开始任务/ })
    expect(cta).toHaveClass('card--primary')
    cta.click()

    // 2. Agent 页：只列出实际已安装的 CLI 并默认选中；主内容无内部术语
    const select = (await screen.findByLabelText('Agent 类型')) as HTMLSelectElement
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(['codex', 'kimi'])
    expect(select.value).toBe('codex')
    for (const banned of JOURNEY_BANNED) expect(mainText()).not.toContain(banned)

    // 3. 首条任务：首次失败 → 用户可读错误（说能做什么）→ 同一按钮重试 → 启动中 → 处理中
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '修复登录回归' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText(/这台工作空间暂时无法启动 Agent/)
    expect(screen.getByText(/再点一次主按钮重试/)).toBeInTheDocument()
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByRole('button', { name: '正在发送…' }) // 启动瞬态
    await waitFor(() => expect(screen.getByTestId('agent-status').textContent).toContain('正在执行'))

    // 4. 回复经有界轮询到达（不靠手动刷新）
    await screen.findByText(/回复：修复登录回归/, undefined, { timeout: 4000 })

    // 5. 刷新：新 render 从 ?agent= 恢复，零新 create
    cleanup()
    renderApp(`${AGENT_ROUTE}?agent=${AGENT_ID}`)
    await screen.findByText(/回复：修复登录回归/)
    expect(backend.creates()).toBe(2) // 1 次首败 + 1 次成功，无新增

    // 6. 第二条：继续发送，只发 prompts
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '补一条回归用例' } })
    screen.getByRole('button', { name: '发送' }).click()
    await screen.findByText(/回复：补一条回归用例/, undefined, { timeout: 4000 })
    expect(backend.calls.filter((c) => c.method === 'POST' && c.url === AGENTS_BASE)).toHaveLength(2)
    for (const banned of JOURNEY_BANNED) expect(mainText()).not.toContain(banned)
  }, 20000)

  it('390px：同一闭环可完成（首页主操作 → 首条任务 → 回复 → 刷新 → 第二条）', async () => {
    ;(window as { innerWidth: number }).innerWidth = 390
    window.dispatchEvent(new Event('resize'))
    const backend = journeyBackend()
    backend.install()

    renderApp('/projects/p1/workspaces/w1')
    const cta = await screen.findByRole('link', { name: /开始任务/ })
    expect(cta).toHaveClass('card--primary')
    cta.click()

    const select = (await screen.findByLabelText('Agent 类型')) as HTMLSelectElement
    expect(select.value).toBe('codex')
    // 390 下 Rail 底部栏仍有 Agent 入口
    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(await within(rail).findByTitle('Agent')).toHaveClass('rail-item--mobile-core')

    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '第一条任务' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText(/回复：第一条任务/, undefined, { timeout: 4000 })

    cleanup()
    renderApp(`${AGENT_ROUTE}?agent=${AGENT_ID}`)
    await screen.findByText(/回复：第一条任务/)
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '第二条任务' } })
    screen.getByRole('button', { name: '发送' }).click()
    await screen.findByText(/回复：第二条任务/, undefined, { timeout: 4000 })
    for (const banned of JOURNEY_BANNED) expect(mainText()).not.toContain(banned)
  }, 20000)
})

// ---------- 收紧项：恢复自动刷新 / 常驻手动刷新 / 示例填入 / 状态与错误用户语言 ----------

describe('AgentPage 收紧项', () => {
  it('?agent= 恢复后 status=idle 自动进入有界刷新（无操作也有后续 GET）；手动刷新入口常驻', async () => {
    const { calls } = await renderAgentWorld(
      {
        detail: () => ({ body: { data: agentView({ status: 'idle', transcript: '既有回复' }), meta: metaOk } }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('既有回复')
    expect(screen.getByRole('button', { name: '刷新' })).toBeInTheDocument()
    const gets = () => calls.filter((c) => c.method === 'GET').length
    const at = gets()
    await waitFor(() => expect(gets()).toBeGreaterThan(at), { timeout: 2500 })
  })

  it('?agent= 恢复后 status=working 同样自动进入有界刷新', async () => {
    const { calls } = await renderAgentWorld(
      {
        detail: () => ({
          body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk },
        }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await waitFor(() => expect(screen.getByTestId('agent-status').textContent).toContain('正在执行'))
    const gets = () => calls.filter((c) => c.method === 'GET').length
    const at = gets()
    await waitFor(() => expect(gets()).toBeGreaterThan(at), { timeout: 2500 })
  })

  it('手动刷新：点击立即 GET（不等轮询间隔）并更新 transcript', async () => {
    let n = 0
    const { calls } = await renderAgentWorld(
      {
        detail: () => {
          n += 1
          return {
            body: { data: agentView(n === 1 ? { transcript: '旧回复' } : { transcript: '新回复' }), meta: metaOk },
          }
        },
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('旧回复')
    const before = calls.filter((c) => c.method === 'GET').length
    screen.getByRole('button', { name: '刷新' }).click()
    // 第一个轮询 tick 在 1s 后才到；800ms 内的 +1 只能来自点击
    await waitFor(
      () => expect(calls.filter((c) => c.method === 'GET').length).toBe(before + 1),
      { timeout: 800 },
    )
    await screen.findByText('新回复')
  })

  it('空 composer 提供无副作用示例：一键填入，不自动发送', async () => {
    const { calls } = await renderAgentWorld({})
    const example = await screen.findByRole('button', { name: /概览这个项目，并说明主要目录的作用/ })
    fireEvent.click(example)
    expect(screen.getByLabelText('任务')).toHaveValue('概览这个项目，并说明主要目录的作用')
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('status=blocked 映射为「需要你处理」', async () => {
    await renderAgentWorld(
      {
        detail: () => ({
          body: { data: agentView({ status: 'blocked', transcript: '请确认是否覆盖配置文件' }), meta: metaOk },
        }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('请确认是否覆盖配置文件')
    await waitFor(() =>
      expect(screen.getByTestId('agent-status').textContent).toContain('需要你处理'),
    )
  })

  it('working 的部分输出不停窗：working+部分输出 → working+更多输出 → idle+最终回复才停止', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({ body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk } }),
      detail: (n) => ({
        body: {
          data: agentView(
            n === 1
              ? { status: 'working', transcript: '部分输出' }
              : n === 2
                ? { status: 'working', transcript: '部分输出\n更多输出' }
                : { status: 'idle', transcript: '部分输出\n更多输出\n最终回复' },
          ),
          meta: metaOk,
        },
      }),
    })
    const gets = () => calls.filter((c) => c.method === 'GET').length
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务E' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/prompts'))).toBe(true))
    // working+部分输出：transcript 变了但窗口不得停
    await screen.findByText(/部分输出/, undefined, { timeout: 2500 })
    const atPartial = gets()
    // 继续轮询拿到 working+更多输出
    await screen.findByText(/更多输出/, undefined, { timeout: 2500 })
    expect(gets()).toBeGreaterThan(atPartial)
    // idle+最终回复：停止；完整回复可见，GET 不再增长
    await screen.findByText(/最终回复/, undefined, { timeout: 2500 })
    await waitFor(() => expect(screen.getByTestId('agent-status').textContent).toContain('空闲'))
    const settled = gets()
    await new Promise((r) => setTimeout(r, 2200))
    expect(gets()).toBe(settled)
  }, 15000)

  it('POST 返回 idle+输入回显不误判完成：baseline 取回显后 transcript，idle 未变化继续轮询', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      // 发送前捕获的 idle：transcript 只含输入回显，不含回复
      prompt: () => ({ body: { data: agentView({ status: 'idle', transcript: '$ 任务F\n' }), meta: metaOk } }),
      detail: (n) => ({
        body: {
          data: agentView(
            n === 1
              ? { status: 'idle', transcript: '$ 任务F\n' } // 快速竞态：仍是 idle+回显
              : n === 2
                ? { status: 'working', transcript: '$ 任务F\n部分回复' }
                : n === 3
                  ? { status: 'working', transcript: '$ 任务F\n部分回复\n更多' }
                  : { status: 'idle', transcript: '$ 任务F\n部分回复\n更多\n最终完整回复' },
          ),
          meta: metaOk,
        },
      }),
    })
    const gets = () => calls.filter((c) => c.method === 'GET').length
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务F' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    // 回显立即可见，但不得停窗（baseline=回显后 transcript，GET1 idle 未变化 → 继续）
    await screen.findByText(/\$ 任务F/)
    const atEcho = gets()
    await screen.findByText(/部分回复/, undefined, { timeout: 2500 })
    expect(gets()).toBeGreaterThan(atEcho) // 没有在输入回显早停
    // working+更多：仍不停
    const atPartial = gets()
    await screen.findByText(/更多/, undefined, { timeout: 2500 })
    expect(gets()).toBeGreaterThan(atPartial) // 没有在部分回复早停
    // idle+最终完整回复 → 停止
    await screen.findByText(/最终完整回复/, undefined, { timeout: 2500 })
    await waitFor(() => expect(screen.getByTestId('agent-status').textContent).toContain('空闲'))
    const settled = gets()
    await new Promise((r) => setTimeout(r, 2200))
    expect(gets()).toBe(settled)
  }, 15000)

  it('后端错误码转用户语言：workspace_agent_unavailable 不作主文案', async () => {
    await renderAgentWorld({
      create: () => ({
        status: 503,
        body: {
          error: { code: 'workspace_agent_unavailable', message: 'workspace agent unavailable', retryable: true },
        },
      }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务D' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText(/这台工作空间暂时无法启动 Agent，请稍后重试/)
    expect(screen.queryByText(/workspace_agent_unavailable/)).not.toBeInTheDocument()
  })

  it('qodercli 已安装但不在本轮白名单：无可提交选项、零 POST、用户语言说明', async () => {
    const envQoderOnly = {
      herdr: { installed: true, path: '/usr/local/bin/herdr' },
      agents: { qodercli: { installed: true, path: '/usr/local/bin/qodercli' } },
      agent_mail: agentMailStatus,
    }
    const { calls } = await renderAgentWorld({ envCheck: envQoderOnly })
    await screen.findByText('当前没有受支持的 Agent')
    expect(screen.getByText(/本轮支持 codex、claude、kimi、opencode、grok/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Agent 类型')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始任务' })).not.toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('可提交类型 = 已安装 ∩ 白名单：qodercli+codex 已装时只可选 codex', async () => {
    const envMixed = {
      herdr: { installed: true, path: '/usr/local/bin/herdr' },
      agents: {
        codex: { installed: true, path: '/usr/local/bin/codex' },
        qodercli: { installed: true, path: '/usr/local/bin/qodercli' },
        kimi: { installed: false, path: '' },
      },
      agent_mail: agentMailStatus,
    }
    await renderAgentWorld({ envCheck: envMixed })
    const select = (await screen.findByLabelText('Agent 类型')) as HTMLSelectElement
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(['codex'])
    expect(select.value).toBe('codex')
  })
})

// ---------- R3：outcome_unknown 待确认 / agent_not_found 断开 / unknown 可发送 ----------

describe('AgentPage R3（backend R3 冻结码）', () => {
  it('agent_send_outcome_unknown：待确认不自动重发、主按钮禁重发；刷新 + 显式确认才换新 key 重发', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: (attempt) =>
        attempt === 1
          ? {
              status: 409,
              body: {
                error: { code: 'agent_send_outcome_unknown', message: 'send outcome unknown', retryable: false },
              },
            }
          : { body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk } },
      detail: () => ({ body: { data: agentView({ status: 'idle', transcript: '' }), meta: metaOk } }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务G' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    // 待确认文案：不显示「任务没有发出去/直接重试」
    await screen.findByText('发送结果待确认')
    expect(screen.getByText(/先点「刷新」确认/)).toBeInTheDocument()
    expect(screen.queryByText(/任务没有发出去/)).not.toBeInTheDocument()
    expect(screen.queryByText(/直接再点一次/)).not.toBeInTheDocument()
    // 主按钮在待确认期间不可造成重发
    expect(screen.getByRole('button', { name: '发送' })).toHaveAttribute('aria-disabled', 'true')
    // 无自动重发（等过一个轮询周期仍只有 1 次 prompts POST）
    await new Promise((r) => setTimeout(r, 1200))
    expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(1)
    // 先刷新确认（未收到：transcript 未变 → 仍待确认）
    fireEvent.click(screen.getAllByRole('button', { name: '刷新' })[0])
    await waitFor(() => expect(calls.some((c) => c.method === 'GET')).toBe(true))
    expect(screen.getByText('发送结果待确认')).toBeInTheDocument()
    // 显式二次动作：生成新 prompt key 并重发
    fireEvent.click(screen.getByRole('button', { name: '确认未收到，重新发送' }))
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(2))
    const posts = calls.filter((c) => c.url.endsWith('/prompts'))
    expect(posts[1].headers['Idempotency-Key']).not.toBe(posts[0].headers['Idempotency-Key'])
    expect(posts[1].body).toEqual({ prompt: '任务G' })
    // 重发成功后待确认解除
    await waitFor(() => expect(screen.queryByText('发送结果待确认')).not.toBeInTheDocument())
  }, 15000)

  it('URL 恢复 agent_not_found：清旧 agent/query、保留任务文本、重新 start-or-attach 后首条与第二条成功', async () => {
    let stored: AgentView | null = null
    let replyPending: string | null = null
    const { calls } = await renderAgentWorld(
      {
        create: () => {
          stored = agentView()
          return { status: 201, body: { data: stored, meta: metaOk } }
        },
        prompt: (attempt) => {
          stored = { ...stored!, status: 'working' }
          replyPending = attempt === 1 ? '回复一' : '回复二'
          return { body: { data: stored, meta: metaOk } }
        },
        detail: () => {
          if (!stored) {
            return { status: 404, body: { error: { code: 'agent_not_found', message: 'no agent', retryable: false } } }
          }
          if (replyPending) {
            stored = {
              ...stored,
              status: 'idle',
              transcript: `${stored.transcript}${stored.transcript ? '\n' : ''}${replyPending}`,
            }
            replyPending = null
          }
          return { body: { data: stored, meta: metaOk } }
        },
      },
      `${AGENT_ROUTE}?agent=ag_gone`,
    )
    // 恢复 404 → 断开出入口；任务文本输入立即可用
    await screen.findByText('Agent 会话已断开')
    fireEvent.click(screen.getByRole('button', { name: '开始新任务' }))
    // 重新 start-or-attach：第一条
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '第一条' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText(/回复一/, undefined, { timeout: 4000 })
    // 第二条
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '第二条' } })
    screen.getByRole('button', { name: '发送' }).click()
    await screen.findByText(/回复二/, undefined, { timeout: 4000 })
    // 旧 agent 只在恢复时出现一次，之后零请求
    expect(calls.filter((c) => c.url.includes('ag_gone'))).toHaveLength(1)
  }, 15000)

  it('轮询中 agent_not_found：停轮询、任务文本保留、回到可重新 start-or-attach', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({ body: { data: agentView({ status: 'working', transcript: '' }), meta: metaOk } }),
      detail: () => ({
        status: 404,
        body: { error: { code: 'agent_not_found', message: 'no agent', retryable: false } },
      }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '保留我' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText('Agent 会话已断开', undefined, { timeout: 3000 })
    // 已发出的任务正常清空；断开后可输入新任务并重新 start-or-attach
    const gets = () => calls.filter((c) => c.method === 'GET').length
    const at = gets()
    await new Promise((r) => setTimeout(r, 2200))
    expect(gets()).toBe(at) // 轮询已停
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '新任务' } })
    const restartBtn = screen.getByRole('button', { name: '开始任务' })
    expect(restartBtn).not.toHaveAttribute('aria-disabled')
    restartBtn.click()
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'POST' && c.url === AGENTS_BASE).length).toBeGreaterThanOrEqual(2),
    )
  }, 15000)

  it('prompt agent_not_found：不重发旧 agent，直接进入断开出入口', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({
        status: 404,
        body: { error: { code: 'agent_not_found', message: 'no agent', retryable: false } },
      }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务H' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await screen.findByText('Agent 会话已断开')
    expect(screen.getByLabelText('任务')).toHaveValue('任务H')
    expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(1)
    expect(screen.getByRole('button', { name: '开始任务' })).toBeInTheDocument()
  })

  it('status=unknown 显示「状态暂不可用」但仍可发送', async () => {
    const { calls } = await renderAgentWorld(
      {
        detail: () => ({ body: { data: agentView({ status: 'unknown', transcript: '旧回复' }), meta: metaOk } }),
        prompt: () => ({ body: { data: agentView({ status: 'working', transcript: '旧回复' }), meta: metaOk } }),
      },
      `${AGENT_ROUTE}?agent=${AGENT_ID}`,
    )
    await screen.findByText('旧回复')
    await waitFor(() => expect(screen.getByTestId('agent-status').textContent).toContain('状态暂不可用'))
    fireEvent.change(screen.getByLabelText('任务'), { target: { value: '继续' } })
    const btn = screen.getByRole('button', { name: '发送' })
    expect(btn).not.toHaveAttribute('aria-disabled')
    btn.click()
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(1))
  })
})

// ---------- 首用恢复（P1-b）与无 Agent 出口 ----------

describe('AgentPage 首用恢复（P1-b）与无 Agent 出口', () => {
  const RECENT_KEY = 'cockpit.recentAgent.v1'
  const RECENT_FIELD = `${REG_P1}/w1`
  const seedRecent = (agentId: string) =>
    window.localStorage.setItem(RECENT_KEY, JSON.stringify({ [RECENT_FIELD]: agentId }))
  const readRecent = () =>
    JSON.parse(window.localStorage.getItem(RECENT_KEY) ?? '{}') as Record<string, string>

  beforeEach(() => {
    window.localStorage.clear()
  })

  it('无记录且无 ?agent=：空 composer，零 agent GET', async () => {
    const { calls } = await renderAgentWorld({})
    await screen.findByLabelText('任务')
    await new Promise((r) => setTimeout(r, 300))
    expect(calls.filter((c) => c.method === 'GET')).toHaveLength(0)
  })

  it('有记录且无 ?agent=：自动转到最近会话并 GET 恢复', async () => {
    seedRecent(AGENT_ID)
    const { calls } = await renderAgentWorld({
      detail: () => ({ body: { data: agentView({ transcript: '历史回复' }), meta: metaOk } }),
    })
    await screen.findByText('历史回复')
    expect(calls.some((c) => c.method === 'GET' && c.url.endsWith(`/agents/${AGENT_ID}`))).toBe(true)
    expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument()
  })

  it('stale 记录：agent_not_found → 清记录、回空 composer、断开提示、无恢复循环', async () => {
    seedRecent(AGENT_ID)
    const { calls } = await renderAgentWorld({
      detail: () => ({
        status: 404,
        body: { error: { code: 'agent_not_found', message: 'gone', retryable: false } },
      }),
    })
    await screen.findByText('Agent 会话已断开')
    expect(readRecent()[RECENT_FIELD]).toBeUndefined()
    expect(await screen.findByLabelText('Agent 类型')).toBeInTheDocument()
    const gets = () => calls.filter((c) => c.method === 'GET').length
    const at = gets()
    await new Promise((r) => setTimeout(r, 1500))
    expect(gets()).toBe(at)
  })

  it('恢复后显式「新任务」：清记录回空 composer，不再自动跳回', async () => {
    seedRecent(AGENT_ID)
    const { calls } = await renderAgentWorld({
      detail: () => ({ body: { data: agentView({ transcript: '历史回复' }), meta: metaOk } }),
    })
    await screen.findByText('历史回复')
    fireEvent.click(screen.getByRole('button', { name: '新任务' }))
    expect(await screen.findByLabelText('Agent 类型')).toBeInTheDocument()
    expect(readRecent()[RECENT_FIELD]).toBeUndefined()
    const gets = () => calls.filter((c) => c.method === 'GET').length
    const at = gets()
    await new Promise((r) => setTimeout(r, 1200))
    expect(gets()).toBe(at)
  })

  it('创建成功写入最近记录', async () => {
    const { calls } = await renderAgentWorld({
      create: () => ({ status: 201, body: { data: agentView(), meta: metaOk } }),
      prompt: () => ({ body: { data: agentView({ status: 'working' }), meta: metaOk } }),
      detail: () => ({ body: { data: agentView({ status: 'working' }), meta: metaOk } }),
    })
    fireEvent.change(await screen.findByLabelText('任务'), { target: { value: '任务X' } })
    screen.getByRole('button', { name: '开始任务' }).click()
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/prompts'))).toBe(true))
    expect(readRecent()[RECENT_FIELD]).toBe(AGENT_ID)
  })

  it('无受支持 Agent：动作化文案 + 「打开环境自检」出口，不泄露路径', async () => {
    await renderAgentWorld({ envCheck: envNonePayload })
    await screen.findByText('还没有可用的 Agent')
    const link = screen.getByRole('link', { name: '打开环境自检' })
    expect(link).toHaveAttribute('href', '#/settings?view=doctor')
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument()
    const text = document.querySelector('main')?.textContent ?? ''
    expect(text).not.toMatch(/\/home|\.config|\/usr\/local/)
  })
})
