import { fireEvent, screen, waitFor } from '@testing-library/react'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'

/**
 * Checkpoint A 产品流 unit 证据（Wiki 37 §1/§4/§6 的可 mock 部分）：
 * - 空态 -> 保存 -> 已保存；刷新（重挂载）/离开再返回同 IDs 原文；
 * - 失败（409/400/断网）保留草稿与同一 intent key，恢复后重试成功；
 * - /agent 深链回到 Workspace Focus；
 * - 全程零 Agent API 调用，不读 transcript，无 A 未实现能力。
 * 真实浏览器/视口/主题/键盘证据在 e2e-live/checkpoint-a-focus.spec.ts。
 */

const WORK_ITEMS_URL = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const HOME_ROUTE = '/projects/p1/workspaces/w1'

const BODY = '修复登录失败'
const ACCEPTANCE = '刷新后仍保持登录'
const CONSTRAINTS = '不要修改现有会话格式'

const AGENT_API = /(createAgent|sendAgentPrompt|agent-prompt|transcript|\/agents|\/claim|\/reply)/i

function savedAggregate() {
  return {
    thread: {
      thread_id: 'thr_w1flow',
      project_id: REG_P1,
      workspace_id: 'w1',
      revision: 1,
      created_at: '2026-08-15T00:00:00+00:00',
    },
    root_message: {
      message_id: 'msg_w1flow',
      thread_id: 'thr_w1flow',
      author_kind: 'boss',
      author_ref: null,
      body: BODY,
    },
    work_item: {
      work_item_id: 'wrk_w1flow',
      source_message_id: 'msg_w1flow',
      status: 'unassigned',
      acceptance: ACCEPTANCE,
      constraints: CONSTRAINTS,
    },
  }
}

interface RecordedRequest {
  url: string
  method: string
  idempotencyKey: string | null
  body: string
}

type WorkItemsResponder = (
  method: string,
  request: RecordedRequest,
) => { status?: number; body: unknown } | undefined

/** method-aware fetch mock：work-items 走 responder，其余 GET 落 defaultFetchMap。 */
function stubFlowFetch(responder: WorkItemsResponder) {
  const defaults = defaultFetchMap()
  const calls: RecordedRequest[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const rawHeaders = init?.headers as Record<string, string> | undefined
    const request: RecordedRequest = {
      url,
      method,
      idempotencyKey: rawHeaders?.['Idempotency-Key'] ?? null,
      body: typeof init?.body === 'string' ? init.body : '',
    }
    calls.push(request)
    let spec: { status?: number; body: unknown } | undefined
    if (url === WORK_ITEMS_URL) {
      spec = responder(method, request)
    } else {
      const key = Object.keys(defaults)
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      spec = key ? { body: defaults[key] } : undefined
      if (!spec && method !== 'GET') spec = { status: 404, body: { error: { code: 'not_found' } } }
    }
    if (!spec) {
      spec = {
        status: 404,
        body: { error: { code: 'not_found', message: `no mock for ${url}` } },
      }
    }
    const status = spec.status ?? 200
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => spec.body,
    } as Response
  }))
  return calls
}

function workItemsList(items: unknown[]) {
  return { data: { items, next_cursor: null }, meta: metaOk }
}

function draftStorageKey(): string {
  return `cockpit.workDraft.v1:${encodeURIComponent(REG_P1)}:w1`
}

function readDraft(): { body: string; intentKey: string } | null {
  const raw = window.localStorage.getItem(draftStorageKey())
  return raw ? (JSON.parse(raw) as { body: string; intentKey: string }) : null
}

async function fillAndSave() {
  fireEvent.change(await screen.findByLabelText('今天想推进什么？'), {
    target: { value: BODY },
  })
  fireEvent.change(screen.getByLabelText('怎样算完成？', { exact: false }), {
    target: { value: ACCEPTANCE },
  })
  fireEvent.change(screen.getByLabelText('需要特别注意什么？', { exact: false }), {
    target: { value: CONSTRAINTS },
  })
  fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Checkpoint A 保存主链（unit）', () => {
  it('空态保存 -> 已保存只显示 Boss 原文；刷新与离开返回后原文不变且不读 transcript', async () => {
    let saved: unknown[] = []
    const calls = stubFlowFetch((method, _request) => {
      if (method === 'POST') {
        const aggregate = savedAggregate()
        saved = [...saved, aggregate]
        return { status: 201, body: { data: aggregate, meta: metaOk } }
      }
      return { body: workItemsList(saved) }
    })

    const first = renderApp(HOME_ROUTE)
    await fillAndSave()

    const post = calls.find((call) => call.method === 'POST')
    expect(post?.url).toBe(WORK_ITEMS_URL)
    expect(post?.idempotencyKey).toMatch(/./)
    expect(JSON.parse(post?.body ?? '{}')).toEqual({
      body: BODY,
      acceptance: ACCEPTANCE,
      constraints: CONSTRAINTS,
    })

    expect(await screen.findByText('工作已保存')).toBeInTheDocument()
    expect(screen.getByText(BODY)).toBeInTheDocument()
    expect(screen.getByText(ACCEPTANCE)).toBeInTheDocument()
    expect(screen.getByText(CONSTRAINTS)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存工作' })).not.toBeInTheDocument()
    expect(readDraft()).toBeNull()

    // “刷新”：卸载后重挂载，GET 回读同一聚合，原文与状态不变。
    first.unmount()
    const second = renderApp(HOME_ROUTE)
    expect(await screen.findByText(BODY)).toBeInTheDocument()
    expect(screen.getByText('工作已保存')).toBeInTheDocument()
    expect(screen.queryByLabelText('今天想推进什么？')).not.toBeInTheDocument()

    // “离开再返回”：路由切到 Files 再回到工作对话。
    fireEvent.click(screen.getByTitle('文件'))
    await waitFor(() => {
      expect(screen.queryByText(BODY)).not.toBeInTheDocument()
    })
    fireEvent.click(screen.getByTitle('工作对话'))
    expect(await screen.findByText(BODY)).toBeInTheDocument()
    expect(screen.getByText('工作已保存')).toBeInTheDocument()
    second.unmount()

    const getReads = calls.filter((call) => call.url === WORK_ITEMS_URL && call.method === 'GET')
    expect(getReads.length).toBeGreaterThanOrEqual(2)
    expect(calls.some((call) => AGENT_API.test(call.url))).toBe(false)
    expect(
      calls.filter((call) => call.method === 'POST').every((call) => call.url === WORK_ITEMS_URL),
    ).toBe(true)
  })

  it('409/400/断网都保留草稿与同一 intent key；恢复后原 key 重试成功', async () => {
    let attempts = 0
    const calls = stubFlowFetch((method, _request) => {
      if (method === 'POST') {
        attempts += 1
        if (attempts === 1) {
          return {
            status: 409,
            body: {
              error: {
                code: 'idempotency_conflict',
                message: 'idempotency conflict',
                retryable: false,
                request_id: 'req-1',
                details: {},
              },
            },
          }
        }
        if (attempts === 2) {
          return {
            status: 400,
            body: {
              error: {
                code: 'invalid_argument',
                message: 'invalid argument',
                retryable: false,
                request_id: 'req-2',
                details: {},
              },
            },
          }
        }
        if (attempts === 3) throw new TypeError('network down')
        const aggregate = savedAggregate()
        return { status: 201, body: { data: aggregate, meta: metaOk } }
      }
      return { body: workItemsList([]) }
    })

    renderApp(HOME_ROUTE)
    await fillAndSave()
    expect(await screen.findByText('保存意图发生冲突。草稿仍保留；修改任一字段后可作为新的工作保存。')).toBeInTheDocument()
    const draftAfter409 = readDraft()
    expect(draftAfter409?.body).toBe(BODY)
    expect(draftAfter409?.intentKey).toMatch(/./)

    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    expect(await screen.findByText('工作内容不符合保存要求。草稿仍保留，请检查后重试。')).toBeInTheDocument()
    expect(readDraft()?.intentKey).toBe(draftAfter409?.intentKey)

    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    expect(await screen.findByText('当前无法连接服务。草稿仍保留在本机，请恢复连接后重试。')).toBeInTheDocument()
    expect(readDraft()?.intentKey).toBe(draftAfter409?.intentKey)
    expect(screen.getByLabelText('今天想推进什么？')).toHaveValue(BODY)

    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    expect(await screen.findByText('工作已保存')).toBeInTheDocument()

    const posts = calls.filter((call) => call.method === 'POST')
    expect(posts).toHaveLength(4)
    expect(new Set(posts.map((call) => call.idempotencyKey)).size).toBe(1)
    expect(readDraft()).toBeNull()
  })

  it('旧 /agent 深链回到 Workspace Focus，零 Agent API、无假能力入口', async () => {
    const calls = stubFlowFetch((_method, _request) => ({ body: workItemsList([]) }))

    renderApp(`${HOME_ROUTE}/agent`)

    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存并开始' })).not.toBeInTheDocument()
    for (const absent of ['选择 Agent', '认领', '回复', 'Checkout', 'SSH', '团队', '远程 GPU']) {
      expect(screen.queryByText(new RegExp(absent))).not.toBeInTheDocument()
    }
    expect(calls.some((call) => AGENT_API.test(call.url))).toBe(false)
    expect(
      calls.filter((call) => call.method === 'POST').every((call) => call.url === WORK_ITEMS_URL),
    ).toBe(true)
  })
})
