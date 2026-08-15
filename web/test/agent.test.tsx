import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import {
  assertAgentView,
  createAgent,
  getAgent,
  sendAgentPrompt,
  type AgentView,
} from '../api/agents'
import { assertWorkspaceWorkAggregate } from '../api/workspaceWork'
import { agentMailStatus, defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'

// 每个测试等同一个全新浏览器：localStorage 跨用例清零
beforeEach(() => {
  window.localStorage.clear()
})

const AGENT_ID = 'ag_test1'
const AGENTS_BASE = `/api/projects/${REG_P1}/workspaces/w1/agents`
const AGENT_ROUTE = '/projects/p1/workspaces/w1/agent'
const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const FOCUS_ROUTE = '/projects/p1/workspaces/w1'
const THREAD_ID = 'th_focus1'
const MESSAGE_ID = 'msg_focus1'
const WORK_ITEM_ID = 'wrk_focus1'
const CREATED_AT = '2026-08-15T00:00:00+00:00'
const AGENT_BANNED = ['Herdr', 'session', 'pane', 'cwd', 'PID', 'argv', 'workdir']
const mainText = () => document.querySelector('main')?.textContent ?? ''

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

/** env-check 变体：一个 Agent CLI 都没装（延期能力，页面不得依赖） */
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

function emptyWorkList() {
  return { data: { items: [] as unknown[], next_cursor: null }, meta: metaOk }
}

function workAggregate(over: {
  body?: string
  acceptance?: string | null
  constraints?: string | null
  messageId?: string
  threadId?: string
} = {}) {
  const threadId = over.threadId ?? THREAD_ID
  const messageId = over.messageId ?? MESSAGE_ID
  return {
    thread: { thread_id: threadId, created_at: CREATED_AT },
    root_message: {
      message_id: messageId,
      thread_id: threadId,
      author_kind: 'boss' as const,
      author_ref: null,
      body: over.body ?? '修复登录失败',
    },
    work_item: {
      work_item_id: WORK_ITEM_ID,
      source_message_id: messageId,
      acceptance: over.acceptance === undefined ? '刷新后仍保持登录' : over.acceptance,
      constraints: over.constraints === undefined ? '不要修改现有会话格式' : over.constraints,
      status: 'unassigned' as const,
    },
  }
}

interface FocusRoutes {
  list?: (call: number) => Spec | undefined
  save?: (attempt: number) => Spec | undefined
  envCheck?: unknown
}

/** Focus 测试世界：只应答 work-items；Agent 路由 404 以便断言零调用 */
function renderFocusWorld(routes: FocusRoutes = {}, initialRoute = FOCUS_ROUTE) {
  const calls: FetchCall[] = []
  const counters = { list: 0, save: 0 }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const call: FetchCall = {
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers ?? {}) as Record<string, string>,
      }
      let spec: Spec | undefined
      if (url.includes('/agents')) {
        calls.push(call)
        spec = {
          status: 404,
          body: { error: { code: 'not_found', message: 'agent deferred', retryable: false } },
        }
      } else if (url === WORK_ITEMS || url.startsWith(`${WORK_ITEMS}?`)) {
        calls.push(call)
        if (method === 'POST') {
          counters.save += 1
          spec = routes.save?.(counters.save) ?? {
            status: 201,
            body: { data: workAggregate({ body: String(call.body?.body ?? '') }), meta: metaOk },
          }
        } else {
          counters.list += 1
          spec = routes.list?.(counters.list) ?? { body: emptyWorkList() }
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
          json: async () => ({
            error: { code: 'not_found', message: `no mock for ${url}`, retryable: false },
          }),
        } as Response
      }
      const status = spec.status ?? 200
      return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
    }),
  )
  const rendered = renderApp(initialRoute)
  return { ...rendered, calls, counters }
}

function agentCalls(calls: FetchCall[]) {
  return calls.filter((c) => c.url.includes('/agents'))
}

function workPosts(calls: FetchCall[]) {
  return calls.filter((c) => c.method === 'POST' && c.url.startsWith(WORK_ITEMS))
}

// ---------- 单元：agents API 守卫（延期模块仍 fail-closed；页面不得调用） ----------

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
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        captured.push({
          url: String(input),
          method: init?.method ?? 'GET',
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          headers: (init?.headers ?? {}) as Record<string, string>,
        })
        return { ok: true, status: 201, json: async () => ({ data: agentView(), meta: metaOk }) } as Response
      }),
    )
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
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        captured.push({
          url: String(input),
          method: init?.method ?? 'GET',
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          headers: (init?.headers ?? {}) as Record<string, string>,
        })
        return { ok: true, status: 200, json: async () => ({ data: agentView(), meta: metaOk }) } as Response
      }),
    )
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

  it('workspace work 聚合 fail-closed：必须 Boss root + source_message_id 对齐', () => {
    expect(assertWorkspaceWorkAggregate(workAggregate())).toBeTruthy()
    expect(() => assertWorkspaceWorkAggregate({ ...workAggregate(), extra: true })).toThrow(/键集/)
    const badAuthor = workAggregate()
    ;(badAuthor.root_message as { author_kind: string }).author_kind = 'agent'
    expect(() => assertWorkspaceWorkAggregate(badAuthor)).toThrow(/author_kind/)
    const badSource = workAggregate()
    badSource.work_item.source_message_id = 'msg_other'
    expect(() => assertWorkspaceWorkAggregate(badSource)).toThrow(/source_message_id/)
  })

  it('workspace work 聚合 fail-closed：2xx 仍拒绝缺失/空 work_item_id 与缺失/空/非法 created_at', () => {
    expect(assertWorkspaceWorkAggregate(workAggregate())).toBeTruthy()

    const missingWorkItemId = workAggregate()
    delete (missingWorkItemId.work_item as { work_item_id?: string }).work_item_id
    expect(() => assertWorkspaceWorkAggregate(missingWorkItemId)).toThrow(/work_item_id/)

    const emptyWorkItemId = workAggregate()
    emptyWorkItemId.work_item.work_item_id = ''
    expect(() => assertWorkspaceWorkAggregate(emptyWorkItemId)).toThrow(/work_item_id/)

    const missingCreatedAt = workAggregate()
    delete (missingCreatedAt.thread as { created_at?: string }).created_at
    expect(() => assertWorkspaceWorkAggregate(missingCreatedAt)).toThrow(/created_at/)

    const emptyCreatedAt = workAggregate()
    emptyCreatedAt.thread.created_at = ''
    expect(() => assertWorkspaceWorkAggregate(emptyCreatedAt)).toThrow(/created_at/)

    const illegalCreatedAt = workAggregate()
    ;(illegalCreatedAt.thread as { created_at: unknown }).created_at = 1755216000
    expect(() => assertWorkspaceWorkAggregate(illegalCreatedAt)).toThrow(/created_at/)
  })
})

// ---------- 页面：Workspace Focus（旧 AgentPage → Local Focus） ----------

describe('AgentPage → Workspace Focus', () => {
  it('Agent 类型选择器隐藏；只问今天想推进什么，不列 CLI', async () => {
    renderFocusWorld({})
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByLabelText('Agent 类型')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始任务' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
  })

  it('一个 CLI 都没装：Focus 仍可用，不出 Agent 表单、零 Agent POST', async () => {
    const { calls } = renderFocusWorld({ envCheck: envNonePayload })
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByText('还没有可用的 Agent')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始任务' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('任务')).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
    expect(workPosts(calls)).toHaveLength(0)
  })

  it('第一次保存工作：POST 精确 {body,acceptance,constraints} + Idempotency-Key；无 Agent API', async () => {
    const { calls } = renderFocusWorld()
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), {
      target: { value: '修复登录回归' },
    })
    fireEvent.change(screen.getByLabelText('怎样算完成？'), { target: { value: '刷新后仍保持登录' } })
    fireEvent.change(screen.getByLabelText('需要特别注意什么？'), {
      target: { value: '不要修改现有会话格式' },
    })
    screen.getByRole('button', { name: '保存工作' }).click()
    await waitFor(() => expect(workPosts(calls)).toHaveLength(1))
    const post = workPosts(calls)[0]
    expect(post.url).toBe(WORK_ITEMS)
    expect(post.body).toEqual({
      body: '修复登录回归',
      acceptance: '刷新后仍保持登录',
      constraints: '不要修改现有会话格式',
    })
    expect(post.headers['Idempotency-Key']).toBeTruthy()
    expect(agentCalls(calls)).toHaveLength(0)
    await screen.findByText('工作已保存')
    expect(screen.getByText('你')).toBeInTheDocument()
  })

  it('刷新恢复：GET work-items 恢复原文与两个可选字段，零 POST、零 Agent', async () => {
    const saved = workAggregate({
      body: '第一轮问题',
      acceptance: '验收A',
      constraints: '约束B',
    })
    const { calls } = renderFocusWorld({
      list: () => ({ body: { data: { items: [saved], next_cursor: null }, meta: metaOk } }),
    })
    await screen.findByText('第一轮问题')
    expect(screen.getByText('验收A')).toBeInTheDocument()
    expect(screen.getByText('约束B')).toBeInTheDocument()
    expect(screen.getByText('工作已保存')).toBeInTheDocument()
    expect(workPosts(calls)).toHaveLength(0)
    expect(agentCalls(calls)).toHaveLength(0)
    expect(screen.queryByRole('button', { name: '保存工作' })).not.toBeInTheDocument()
  })

  it('已保存后不开放 reply/第二条 prompt：无发送按钮、零 Agent POST', async () => {
    const { calls } = renderFocusWorld({
      list: () => ({
        body: { data: { items: [workAggregate({ body: '第一轮回复内容' })], next_cursor: null }, meta: metaOk },
      }),
    })
    await screen.findByText('第一轮回复内容')
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('任务')).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('空任务：主按钮禁用且零 POST', async () => {
    const { calls } = renderFocusWorld()
    const btn = await screen.findByRole('button', { name: '保存工作' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    btn.click()
    expect(workPosts(calls)).toHaveLength(0)
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('保存 503 同 key 第二次成功：重试复用同一 Idempotency-Key 与 byte-equivalent body', async () => {
    const { calls } = renderFocusWorld({
      save: (attempt) =>
        attempt === 1
          ? {
              status: 503,
              body: {
                error: { code: 'server_error', message: 'temporarily unavailable', retryable: true },
              },
            }
          : { status: 201, body: { data: workAggregate({ body: '任务A' }), meta: metaOk } },
    })
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务A' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText(/草稿仍保留/)
    screen.getByRole('button', { name: '保存工作' }).click()
    await waitFor(() => expect(workPosts(calls)).toHaveLength(2))
    const posts = workPosts(calls)
    expect(posts[1].headers['Idempotency-Key']).toBe(posts[0].headers['Idempotency-Key'])
    expect(JSON.stringify(posts[1].body)).toBe(JSON.stringify(posts[0].body))
    await screen.findByText('工作已保存')
  })

  it('保存成功后不再有界 Agent 轮询：GET 只发生在 work-items 列表', async () => {
    const { calls } = renderFocusWorld()
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务B' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText('工作已保存')
    const gets = () => calls.filter((c) => c.method === 'GET' && c.url.startsWith(WORK_ITEMS)).length
    const settled = gets()
    await new Promise((r) => setTimeout(r, 400))
    expect(gets()).toBe(settled)
    expect(agentCalls(calls)).toHaveLength(0)
    expect(screen.queryByTestId('agent-status')).not.toBeInTheDocument()
  })

  it('卸载后不再有 GET', async () => {
    const { calls, unmount } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    const gets = () => calls.filter((c) => c.method === 'GET').length
    unmount()
    const at = gets()
    await new Promise((r) => setTimeout(r, 400))
    expect(gets()).toBe(at)
  })

  it('入口：工作区首页就是 Focus；Rail 是工作对话不是 Agent', async () => {
    renderFocusWorld({}, FOCUS_ROUTE)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /开始任务/ })).not.toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(within(rail).queryByTitle('Agent')).not.toBeInTheDocument()
    expect(within(rail).getByTitle('工作对话')).toHaveAttribute('href', FOCUS_ROUTE)
    expect(within(rail).getByTitle('文件')).toBeInTheDocument()
    expect(within(rail).getByTitle('终端')).toBeInTheDocument()
  })

  it('390 窄屏：主输入/保存工作/Rail 项目·工作对话·文件·终端可发现', async () => {
    ;(window as { innerWidth: number }).innerWidth = 390
    window.dispatchEvent(new Event('resize'))
    renderFocusWorld()
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(within(rail).queryByTitle('Agent')).not.toBeInTheDocument()
    for (const title of ['需要你处理', '提问与回复', '设置']) {
      expect(within(rail).queryByTitle(title)).not.toBeInTheDocument()
    }
    for (const title of ['项目', '工作对话', '文件', '终端']) {
      expect(within(rail).getByTitle(title)).toHaveClass('rail-item--mobile-core')
    }
  })
})

function journeyBackend(opts: { failFirstSave?: boolean } = {}) {
  let stored: ReturnType<typeof workAggregate> | null = null
  let saveAttempts = 0
  const calls: FetchCall[] = []
  const install = () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        let spec: Spec | undefined
        if (url.includes('/agents')) {
          calls.push({
            url,
            method,
            body: init?.body ? JSON.parse(String(init.body)) : undefined,
            headers: (init?.headers ?? {}) as Record<string, string>,
          })
          spec = {
            status: 404,
            body: { error: { code: 'not_found', message: 'agent deferred', retryable: false } },
          }
        } else if (url === WORK_ITEMS || url.startsWith(`${WORK_ITEMS}?`)) {
          calls.push({
            url,
            method,
            body: init?.body ? JSON.parse(String(init.body)) : undefined,
            headers: (init?.headers ?? {}) as Record<string, string>,
          })
          if (method === 'POST') {
            saveAttempts += 1
            const body = String((JSON.parse(String(init!.body)) as { body: string }).body)
            if (opts.failFirstSave && saveAttempts === 1) {
              spec = {
                status: 503,
                body: { error: { code: 'server_error', message: 'unavailable', retryable: true } },
              }
            } else {
              stored = workAggregate({ body, acceptance: null, constraints: null })
              spec = { status: 201, body: { data: stored, meta: metaOk } }
            }
          } else {
            spec = {
              body: { data: { items: stored ? [stored] : [], next_cursor: null }, meta: metaOk },
            }
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
            json: async () => ({
              error: { code: 'not_found', message: `no mock for ${url}`, retryable: false },
            }),
          } as Response
        }
        const status = spec.status ?? 200
        return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
      }),
    )
  }
  return { install, calls, saves: () => saveAttempts }
}

describe('AgentPage 首用旅程 → Focus 保存/刷新（产品验收）', () => {
  it('新用户闭环：空态 → 保存（含错误重试）→ 已保存 → 刷新 → 离页返回；无第二条 reply', async () => {
    const backend = journeyBackend({ failFirstSave: true })
    backend.install()

    renderApp(FOCUS_ROUTE)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /开始任务/ })).not.toBeInTheDocument()
    for (const banned of AGENT_BANNED) expect(mainText()).not.toContain(banned)

    fireEvent.change(screen.getByLabelText('今天想推进什么？'), { target: { value: '修复登录回归' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText(/草稿仍保留/)
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText('工作已保存')
    expect(screen.getByText('修复登录回归')).toBeInTheDocument()

    cleanup()
    renderApp(FOCUS_ROUTE)
    await screen.findByText('修复登录回归')
    expect(screen.getByText('工作已保存')).toBeInTheDocument()
    expect(backend.saves()).toBe(2)

    cleanup()
    renderApp('/projects/p1/workspaces/w1/files')
    await screen.findByTitle('文件')
    cleanup()
    renderApp(FOCUS_ROUTE)
    await screen.findByText('修复登录回归')
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
    expect(agentCalls(backend.calls)).toHaveLength(0)
    for (const banned of AGENT_BANNED) expect(mainText()).not.toContain(banned)
  })

  it('390px：同一闭环可完成（空态 → 保存 → 刷新）；Rail 无 Agent', async () => {
    ;(window as { innerWidth: number }).innerWidth = 390
    window.dispatchEvent(new Event('resize'))
    const backend = journeyBackend()
    backend.install()

    renderApp(FOCUS_ROUTE)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(within(rail).queryByTitle('Agent')).not.toBeInTheDocument()
    expect(within(rail).getByTitle('工作对话')).toHaveClass('rail-item--mobile-core')

    fireEvent.change(screen.getByLabelText('今天想推进什么？'), { target: { value: '第一条任务' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText('第一条任务')

    cleanup()
    renderApp(AGENT_ROUTE)
    await screen.findByText('第一条任务')
    expect(screen.queryByLabelText('Agent 类型')).not.toBeInTheDocument()
    expect(agentCalls(backend.calls)).toHaveLength(0)
    for (const banned of AGENT_BANNED) expect(mainText()).not.toContain(banned)
  })
})

describe('AgentPage 收紧项 → Focus 隐藏/替代', () => {
  it('?agent= 深链回到 Focus，不自动 GET Agent、无手动刷新 Agent 入口', async () => {
    const { calls } = renderFocusWorld({}, `${AGENT_ROUTE}?agent=${AGENT_ID}`)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '刷新' })).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('旧 working 状态不出现：Focus 无 agent-status', async () => {
    renderFocusWorld({}, `${AGENT_ROUTE}?agent=${AGENT_ID}`)
    await screen.findByLabelText('今天想推进什么？')
    expect(screen.queryByTestId('agent-status')).not.toBeInTheDocument()
    expect(screen.queryByText('正在执行')).not.toBeInTheDocument()
  })

  it('无 Agent 手动刷新：已保存态只靠 GET list，点击不会发 Agent GET', async () => {
    const { calls } = renderFocusWorld({
      list: () => ({
        body: { data: { items: [workAggregate({ body: '旧回复' })], next_cursor: null }, meta: metaOk },
      }),
    })
    await screen.findByText('旧回复')
    expect(screen.queryByRole('button', { name: '刷新' })).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('空 composer 无副作用示例：不提供一键填入、零 POST', async () => {
    const { calls } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    expect(screen.queryByRole('button', { name: /概览这个项目/ })).not.toBeInTheDocument()
    expect(workPosts(calls)).toHaveLength(0)
  })

  it('status=blocked 映射不再出现：无「需要你处理」Agent 状态', async () => {
    renderFocusWorld({
      list: () => ({
        body: {
          data: { items: [workAggregate({ body: '请确认是否覆盖配置文件' })], next_cursor: null },
          meta: metaOk,
        },
      }),
    })
    await screen.findByText('请确认是否覆盖配置文件')
    expect(screen.queryByTestId('agent-status')).not.toBeInTheDocument()
    expect(screen.queryByText('需要你处理')).not.toBeInTheDocument()
  })

  it('不实现 transcript 流式输出：保存后静态显示，无 working 轮询', async () => {
    const { calls } = renderFocusWorld()
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务E' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText('工作已保存')
    expect(screen.queryByText('部分输出')).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('不把输入回显当完成：失败保留草稿，成功才清 composer', async () => {
    const { calls } = renderFocusWorld({
      save: () => ({
        status: 503,
        body: { error: { code: 'server_error', message: 'unavailable', retryable: true } },
      }),
    })
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务F' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText(/草稿仍保留/)
    expect(screen.getByLabelText('今天想推进什么？')).toHaveValue('任务F')
    expect(workPosts(calls)).toHaveLength(1)
  })

  it('后端错误码转用户语言：不把内部码作主文案', async () => {
    renderFocusWorld({
      save: () => ({
        status: 503,
        body: {
          error: {
            code: 'workspace_agent_unavailable',
            message: 'workspace agent unavailable',
            retryable: true,
          },
        },
      }),
    })
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务D' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText(/草稿仍保留/)
    expect(screen.queryByText(/workspace_agent_unavailable/)).not.toBeInTheDocument()
  })

  it('qodercli 白名单不再挡 Focus：无 Agent 类型、Focus 可保存', async () => {
    const envQoderOnly = {
      herdr: { installed: true, path: '/usr/local/bin/herdr' },
      agents: { qodercli: { installed: true, path: '/usr/local/bin/qodercli' } },
      agent_mail: agentMailStatus,
    }
    const { calls } = renderFocusWorld({ envCheck: envQoderOnly })
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByText('当前没有受支持的 Agent')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Agent 类型')).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('可提交类型交集隐藏：不出现 Agent 下拉，只保留保存工作', async () => {
    const envMixed = {
      herdr: { installed: true, path: '/usr/local/bin/herdr' },
      agents: {
        codex: { installed: true, path: '/usr/local/bin/codex' },
        qodercli: { installed: true, path: '/usr/local/bin/qodercli' },
        kimi: { installed: false, path: '' },
      },
      agent_mail: agentMailStatus,
    }
    renderFocusWorld({ envCheck: envMixed })
    expect(await screen.findByRole('button', { name: '保存工作' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Agent 类型')).not.toBeInTheDocument()
  })
})

describe('AgentPage R3 → 延期（不假实现 claim/reply/outcome）', () => {
  it('agent_send_outcome_unknown 待确认流不出现；保存冲突只保留草稿', async () => {
    const { calls } = renderFocusWorld({
      save: () => ({
        status: 409,
        body: { error: { code: 'conflict', message: 'send outcome unknown', retryable: false } },
      }),
    })
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务G' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText(/草稿仍保留/)
    expect(screen.queryByText('发送结果待确认')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认未收到，重新发送' })).not.toBeInTheDocument()
    expect(workPosts(calls)).toHaveLength(1)
  })

  it('URL 恢复 agent_not_found：/agent?agent=gone 回到 Focus，零 Agent GET', async () => {
    const { calls } = renderFocusWorld({}, `${AGENT_ROUTE}?agent=ag_gone`)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByText('Agent 会话已断开')).not.toBeInTheDocument()
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('不轮询 Agent：断开/not_found 不会启动 Agent GET', async () => {
    const { calls } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    const at = agentCalls(calls).length
    await new Promise((r) => setTimeout(r, 400))
    expect(agentCalls(calls)).toHaveLength(at)
  })

  it('不发旧 agent prompt：页面无 prompts 调用', async () => {
    const { calls } = renderFocusWorld()
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务H' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await waitFor(() => expect(workPosts(calls)).toHaveLength(1))
    expect(calls.filter((c) => c.url.endsWith('/prompts'))).toHaveLength(0)
  })

  it('status=unknown 不出现；已保存后不可再发 Agent', async () => {
    renderFocusWorld({
      list: () => ({
        body: { data: { items: [workAggregate({ body: '旧回复' })], next_cursor: null }, meta: metaOk },
      }),
    })
    await screen.findByText('旧回复')
    expect(screen.queryByText('状态暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
  })
})

describe('AgentPage 首用恢复（P1-b）→ last workspace / 无 Agent 出口', () => {
  const RECENT_KEY = 'cockpit.recentAgent.v1'
  const RECENT_FIELD = `${REG_P1}/w1`
  const seedRecent = (agentId: string) =>
    window.localStorage.setItem(RECENT_KEY, JSON.stringify({ [RECENT_FIELD]: agentId }))

  it('无记录且无 ?agent=：空 composer，零 agent GET', async () => {
    const { calls } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    await new Promise((r) => setTimeout(r, 200))
    expect(agentCalls(calls)).toHaveLength(0)
  })

  it('有 recentAgent 记录也不自动恢复 Agent：只走 Focus GET', async () => {
    seedRecent(AGENT_ID)
    const { calls } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    expect(calls.some((c) => c.url.endsWith(`/agents/${AGENT_ID}`))).toBe(false)
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
  })

  it('stale recentAgent：不进入断开循环，Focus 空态可用', async () => {
    seedRecent(AGENT_ID)
    const { calls } = renderFocusWorld()
    await screen.findByLabelText('今天想推进什么？')
    expect(screen.queryByText('Agent 会话已断开')).not.toBeInTheDocument()
    const at = agentCalls(calls).length
    await new Promise((r) => setTimeout(r, 300))
    expect(agentCalls(calls)).toHaveLength(at)
  })

  it('已保存后无「新任务」回 Agent composer', async () => {
    renderFocusWorld({
      list: () => ({
        body: { data: { items: [workAggregate({ body: '历史回复' })], next_cursor: null }, meta: metaOk },
      }),
    })
    await screen.findByText('历史回复')
    expect(screen.queryByRole('button', { name: '新任务' })).not.toBeInTheDocument()
  })

  it('保存成功写入 last workspace，而不是 recentAgent', async () => {
    renderFocusWorld()
    fireEvent.change(await screen.findByLabelText('今天想推进什么？'), { target: { value: '任务X' } })
    screen.getByRole('button', { name: '保存工作' }).click()
    await screen.findByText('工作已保存')
    const last = JSON.parse(window.localStorage.getItem('cockpit.lastWorkspace.v1') ?? '{}') as Record<
      string,
      string
    >
    expect(last[REG_P1]).toBe('w1')
    const recent = JSON.parse(window.localStorage.getItem(RECENT_KEY) ?? '{}') as Record<string, string>
    expect(recent[RECENT_FIELD]).toBeUndefined()
  })

  it('无受支持 Agent：不跳环境自检；Focus 仍是主出口', async () => {
    renderFocusWorld({ envCheck: envNonePayload })
    await screen.findByLabelText('今天想推进什么？')
    expect(screen.queryByRole('link', { name: '打开环境自检' })).not.toBeInTheDocument()
    const text = document.querySelector('main')?.textContent ?? ''
    expect(text).not.toMatch(/\/home|\.config|\/usr\/local/)
  })
})
