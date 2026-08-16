import { fireEvent, screen, waitFor } from '@testing-library/react'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { ProtocolError } from '../api/client'
import { assertWorkspaceDispatchResult } from '../api/workspaceDispatch'
import { renderApp } from './helpers'

const WORK_ITEMS_URL = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const MEMBERS_URL = `/api/projects/${REG_P1}/workspaces/w1/members`
const PREP_URL = `${WORK_ITEMS_URL}/wrk_dispatch/preparation`
const DETAIL_URL = `${WORK_ITEMS_URL}/wrk_dispatch`
const DISPATCH_URL = `${DETAIL_URL}/dispatch`
const HOME_ROUTE = '/projects/p1/workspaces/w1?work=wrk_dispatch'
const FORBIDDEN = /(createAgent|sendAgentPrompt|agent-prompt|transcript|\/agents|\/claim|\/reply|pane_send|pane_read)/i

const member = {
  identity_id: 'idn_dispatch',
  display_name: 'Atlas',
  role: 'member',
  lifecycle: 'active',
  revision: 1,
}

function workAggregate() {
  return {
    thread: {
      thread_id: 'thr_dispatch',
      project_id: REG_P1,
      workspace_id: 'w1',
      revision: 1,
      created_at: '2026-08-16T00:00:00+00:00',
    },
    root_message: {
      message_id: 'msg_dispatch',
      thread_id: 'thr_dispatch',
      author_kind: 'boss',
      author_ref: null,
      body: '完成派遣闭环',
    },
    work_item: {
      work_item_id: 'wrk_dispatch',
      source_message_id: 'msg_dispatch',
      status: 'unassigned',
      acceptance: null,
      constraints: null,
    },
  }
}

function preparation(state: 'prepared' | 'connected_readonly' = 'connected_readonly') {
  return {
    work_item_id: 'wrk_dispatch',
    state,
    revision: state === 'connected_readonly' ? 7 : 6,
    work_item_status: 'unassigned',
    identity: member,
    principal: { identity_id: 'idn_dispatch', generation: 2 },
    checkout: {
      checkout_id: 'chk_dispatch',
      status: 'ready',
      source_head: 'a'.repeat(40),
      source_tree: 'b'.repeat(40),
      ref_kind: 'detached',
      revision: 1,
    },
    lease: { lease_id: 'les_dispatch', status: 'reserved', generation: 2, revision: 1 },
    attachment: state === 'connected_readonly'
      ? {
          attachment_id: 'att_dispatch',
          status: 'connected_readonly',
          provider: 'local_herdr',
          harness: 'codex_terminal_managed_v1',
          generation: 2,
          identity_verified: true,
          revision: 1,
        }
      : null,
  }
}

function detail() {
  return {
    thread: {
      thread_id: 'thr_dispatch',
      project_id: REG_P1,
      workspace_id: 'w1',
      revision: 1,
      created_at: '2026-08-16T00:00:00+00:00',
      messages: [],
    },
    work_item: {
      work_item_id: 'wrk_dispatch',
      source_message_id: 'msg_dispatch',
      status: 'unassigned',
      acceptance: null,
      constraints: null,
      revision: 3,
      updated_at: '2026-08-16T00:00:00+00:00',
    },
    claim: null,
    receipts: [],
  }
}

interface RecordedRequest {
  url: string
  method: string
  body: string
  idempotencyKey: string | null
}

function stubDispatch(options: {
  prepState?: 'prepared' | 'connected_readonly'
  firstDispatchError?: { status: number; code: string; retryable?: boolean }
  holdDispatch?: boolean
} = {}) {
  const calls: RecordedRequest[] = []
  let dispatchCount = 0
  let releaseDispatch: (() => void) | null = null
  const defaults = defaultFetchMap()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const headers = init?.headers as Record<string, string> | undefined
    calls.push({
      url,
      method,
      body: typeof init?.body === 'string' ? init.body : '',
      idempotencyKey: headers?.['Idempotency-Key'] ?? null,
    })
    const response = (body: unknown, status = 200) => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    }) as Response
    if (url === WORK_ITEMS_URL && method === 'GET') {
      return response({ data: { items: [workAggregate()], next_cursor: null }, meta: metaOk })
    }
    if (url === MEMBERS_URL && method === 'GET') {
      return response({ data: { items: [member], next_cursor: null }, meta: metaOk })
    }
    if (url === PREP_URL && method === 'GET') {
      return response({ data: preparation(options.prepState), meta: metaOk })
    }
    if (url === DETAIL_URL && method === 'GET') {
      return response({ data: detail(), meta: metaOk })
    }
    if (url === DISPATCH_URL && method === 'POST') {
      dispatchCount += 1
      if (options.holdDispatch) {
        await new Promise<void>((resolve) => { releaseDispatch = resolve })
      }
      if (dispatchCount === 1 && options.firstDispatchError) {
        const error = options.firstDispatchError
        return response({
          error: {
            code: error.code,
            message: error.code,
            retryable: error.retryable ?? false,
            request_id: 'req-dispatch',
            details: {},
          },
        }, error.status)
      }
      return response({ data: { operation_id: 'op_dispatch', outcome: 'succeeded' }, meta: metaOk })
    }
    const fallback = defaults[url]
    if (fallback) return response(fallback)
    return response({
      error: { code: 'not_found', message: `no mock for ${url}`, retryable: false, request_id: 'req-404', details: {} },
    }, 404)
  }))
  return {
    calls,
    release: () => releaseDispatch?.(),
  }
}

function dispatchPosts(calls: RecordedRequest[]) {
  return calls.filter((call) => call.url === DISPATCH_URL && call.method === 'POST')
}

describe('D-W2 workspace dispatch client', () => {
  it('严格接受 {operation_id,outcome:succeeded}，拒绝未知键和其他 outcome', () => {
    expect(assertWorkspaceDispatchResult({ operation_id: 'op_1', outcome: 'succeeded' })).toEqual({
      operation_id: 'op_1',
      outcome: 'succeeded',
    })
    expect(() => assertWorkspaceDispatchResult({ operation_id: 'op_1', outcome: 'succeeded', pane_id: 'p1' }))
      .toThrow(ProtocolError)
    expect(() => assertWorkspaceDispatchResult({ operation_id: 'op_1', outcome: 'working' }))
      .toThrow(ProtocolError)
  })
})

describe('D-W2 派遣动作', () => {
  it('只读连接完成后才出现唯一主动作；POST 使用双 revision 和 Idempotency-Key', async () => {
    const prepared = stubDispatch({ prepState: 'prepared' })
    const first = renderApp(HOME_ROUTE)
    expect(await screen.findByRole('button', { name: '连接只读 Agent' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '派遣任务' })).not.toBeInTheDocument()
    expect(dispatchPosts(prepared.calls)).toHaveLength(0)
    first.unmount()

    vi.unstubAllGlobals()
    const connected = stubDispatch()
    renderApp(HOME_ROUTE)
    const button = await screen.findByRole('button', { name: '派遣任务' })
    expect(button).toHaveClass('btn--primary')
    expect(screen.getByRole('button', { name: '断开' })).not.toHaveClass('btn--primary')
    fireEvent.click(button)
    await screen.findByText('派遣已提交，等待最新状态。')

    const posts = dispatchPosts(connected.calls)
    expect(posts).toHaveLength(1)
    expect(posts[0].idempotencyKey).toMatch(/./)
    expect(JSON.parse(posts[0].body)).toEqual({
      expected_work_revision: 3,
      expected_preparation_revision: 7,
    })
    expect(screen.getByRole('button', { name: '派遣已提交' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByText(/working|工作中/i)).not.toBeInTheDocument()
    expect(connected.calls.some((call) => FORBIDDEN.test(call.url))).toBe(false)
  })

  it('同步双击只发一次 dispatch', async () => {
    const stub = stubDispatch({ holdDispatch: true })
    renderApp(HOME_ROUTE)
    const button = await screen.findByRole('button', { name: '派遣任务' })
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(dispatchPosts(stub.calls)).toHaveLength(1))
    stub.release()
    await screen.findByText('派遣已提交，等待最新状态。')
  })

  it('wakeup_outcome_unknown 诚实显示结果未知，同 key/body 安全重试', async () => {
    const stub = stubDispatch({
      firstDispatchError: { status: 503, code: 'wakeup_outcome_unknown', retryable: true },
    })
    renderApp(HOME_ROUTE)
    fireEvent.click(await screen.findByRole('button', { name: '派遣任务' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('派遣结果未知，可安全重试。')
    fireEvent.click(screen.getByRole('button', { name: '重试派遣' }))
    await screen.findByText('派遣已提交，等待最新状态。')

    const posts = dispatchPosts(stub.calls)
    expect(posts).toHaveLength(2)
    expect(posts[1].idempotencyKey).toBe(posts[0].idempotencyKey)
    expect(posts[1].body).toBe(posts[0].body)
    expect(stub.calls.filter((call) => call.url === DETAIL_URL && call.method === 'GET')).toHaveLength(1)
  })

  it('typed failure 保留派遣意图，不显示 working', async () => {
    const stub = stubDispatch({
      firstDispatchError: { status: 409, code: 'stale_revision' },
    })
    renderApp(HOME_ROUTE)
    fireEvent.click(await screen.findByRole('button', { name: '派遣任务' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('任务或准备状态已变化，请刷新后重试。')
    expect(screen.getByRole('button', { name: '重试派遣' })).toBeEnabled()
    expect(screen.queryByText(/working|工作中/i)).not.toBeInTheDocument()
    expect(dispatchPosts(stub.calls)).toHaveLength(1)
  })
})
