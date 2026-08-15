import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'

/**
 * 用户验收增强 · 同一 Workspace 多项任务（冻结需求，base 上产品尚未实现）：
 * 保存第一项 -> 新建第二项 -> 列表可见并可来回切换；刷新从 GET 恢复；
 * URL 合法 work id 保持选择，非法 id 安全回退；失败保留草稿与同一 intent key；
 * 双击不重复；零 Agent API。
 */

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const HOME = '/projects/p1/workspaces/w1'
const AGENT_URL = /\/agents|createAgent|sendAgentPrompt|transcript|\/claim|\/reply/i

const FIRST = {
  body: '修登录过期',
  acceptance: '刷新后仍保持登录',
  constraints: '不要改会话格式',
}
const SECOND = {
  body: '补注册校验',
  acceptance: '错误邮箱被拒绝',
  constraints: '不改既有用户表',
}

interface Recorded {
  url: string
  method: string
  idempotencyKey: string | null
  body: string
}

function aggregate(
  id: string,
  fields: { body: string; acceptance: string; constraints: string },
) {
  return {
    thread: { thread_id: `thr_${id}` },
    root_message: {
      message_id: `msg_${id}`,
      thread_id: `thr_${id}`,
      author_kind: 'boss',
      author_ref: null,
      body: fields.body,
    },
    work_item: {
      work_item_id: id,
      source_message_id: `msg_${id}`,
      status: 'unassigned',
      acceptance: fields.acceptance,
      constraints: fields.constraints,
    },
  }
}

function listBody(items: unknown[]) {
  return { data: { items, next_cursor: null }, meta: metaOk }
}

function stubWorkWorld(opts: {
  initial?: unknown[]
  onPost?: (req: Recorded, attempt: number) => { status?: number; body: unknown }
} = {}) {
  const stored = [...(opts.initial ?? [])]
  const calls: Recorded[] = []
  let posts = 0
  const defaults = defaultFetchMap()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const headers = (init?.headers ?? {}) as Record<string, string>
      const rec: Recorded = {
        url,
        method,
        idempotencyKey: headers['Idempotency-Key'] ?? null,
        body: typeof init?.body === 'string' ? init.body : '',
      }
      calls.push(rec)
      let spec: { status?: number; body: unknown } | undefined
      if (url === WORK_ITEMS || url.startsWith(`${WORK_ITEMS}?`)) {
        if (method === 'POST') {
          posts += 1
          spec = opts.onPost
            ? opts.onPost(rec, posts)
            : (() => {
                const parsed = JSON.parse(rec.body) as {
                  body: string
                  acceptance: string | null
                  constraints: string | null
                }
                const id = posts === 1 ? 'wrk_one' : 'wrk_two'
                const item = aggregate(id, {
                  body: parsed.body,
                  acceptance: parsed.acceptance ?? '',
                  constraints: parsed.constraints ?? '',
                })
                stored.push(item)
                return { status: 201, body: { data: item, meta: metaOk } }
              })()
        } else {
          spec = { body: listBody(stored) }
        }
      } else {
        const key = Object.keys(defaults)
          .filter((k) => url === k || url.startsWith(`${k}?`))
          .sort((a, b) => b.length - a.length)[0]
        spec = key ? { body: defaults[key] } : undefined
      }
      spec ??= {
        status: 404,
        body: { error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } },
      }
      const status = spec.status ?? 200
      return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
    }),
  )
  return { calls, stored }
}

function draftKey() {
  return `cockpit.workDraft.v1:${encodeURIComponent(REG_P1)}:w1`
}

function readDraft(): { body: string; intentKey: string } | null {
  const raw = window.localStorage.getItem(draftKey())
  return raw ? (JSON.parse(raw) as { body: string; intentKey: string }) : null
}

async function fillFields(fields: { body: string; acceptance: string; constraints: string }) {
  fireEvent.change(await screen.findByLabelText('今天想推进什么？'), {
    target: { value: fields.body },
  })
  fireEvent.change(screen.getByLabelText('怎样算完成？', { exact: false }), {
    target: { value: fields.acceptance },
  })
  fireEvent.change(screen.getByLabelText('需要特别注意什么？', { exact: false }), {
    target: { value: fields.constraints },
  })
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('验收 v2 · 同一 Workspace 多项任务', () => {
  it('保存第一项后可新建第二项，列表可见并能来回切换各自原文/说明', async () => {
    const user = userEvent.setup()
    const { calls } = stubWorkWorld()
    renderApp(HOME)

    await fillFields(FIRST)
    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    expect(await screen.findByText(FIRST.body)).toBeInTheDocument()

    const createAgain = await screen.findByRole('button', { name: '新建工作' })
    await user.click(createAgain)
    await fillFields(SECOND)
    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    expect(await screen.findByText(SECOND.body)).toBeInTheDocument()

    const workList = await screen.findByRole('list', { name: /工作|任务/ })
    const entries = within(workList).getAllByRole('button')
    expect(entries.map((el) => el.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining(FIRST.body), expect.stringContaining(SECOND.body)]),
    )
    expect(entries.length).toBeGreaterThanOrEqual(2)

    await user.click(within(workList).getByRole('button', { name: new RegExp(FIRST.body) }))
    expect(screen.getByText(FIRST.acceptance)).toBeInTheDocument()
    expect(screen.queryByText(SECOND.acceptance)).not.toBeInTheDocument()

    await user.click(within(workList).getByRole('button', { name: new RegExp(SECOND.body) }))
    expect(screen.getByText(SECOND.acceptance)).toBeInTheDocument()
    expect(screen.queryByText(FIRST.acceptance)).not.toBeInTheDocument()
    expect(screen.queryByText(SECOND.constraints)).toBeInTheDocument()

    expect(calls.some((c) => AGENT_URL.test(c.url))).toBe(false)
    expect(screen.queryByText(/正在执行|工作中|已完成任务/)).not.toBeInTheDocument()
  })

  it('刷新/卸载重挂后 GET 恢复两项；合法 work id 保持选择，非法 id 安全回退', async () => {
    const items = [aggregate('wrk_one', FIRST), aggregate('wrk_two', SECOND)]
    const { calls } = stubWorkWorld({ initial: items })

    renderApp(`${HOME}?work=wrk_two`)
    expect(await screen.findByText(SECOND.body)).toBeInTheDocument()
    expect(screen.getByText(SECOND.acceptance)).toBeInTheDocument()
    expect(screen.queryByText(FIRST.acceptance)).not.toBeInTheDocument()
    const selected = screen.getByRole('button', { name: new RegExp(SECOND.body) })
    expect(selected).toHaveAttribute('aria-current', 'true')

    cleanup()
    renderApp(HOME)
    const list = await screen.findByRole('list', { name: /工作|任务/ })
    expect(within(list).getByRole('button', { name: new RegExp(FIRST.body) })).toBeInTheDocument()
    expect(within(list).getByRole('button', { name: new RegExp(SECOND.body) })).toBeInTheDocument()

    cleanup()
    renderApp(`${HOME}?work=not-a-real-id`)
    expect(await screen.findByRole('list', { name: /工作|任务/ })).toBeInTheDocument()
    expect(screen.queryByText('工作空间不存在或不属于当前项目')).not.toBeInTheDocument()
    expect(screen.queryByText(/no mock/i)).not.toBeInTheDocument()
    expect(screen.getByText(FIRST.body)).toBeInTheDocument()
    expect(calls.filter((c) => c.url.startsWith(WORK_ITEMS) && c.method === 'GET').length).toBeGreaterThan(0)
    expect(calls.some((c) => AGENT_URL.test(c.url))).toBe(false)
  })

  it('保存失败保留草稿和同一 intent key；双击仍只发一次 POST', async () => {
    let failOnce = true
    const { calls } = stubWorkWorld({
      onPost: (req, attempt) => {
        if (failOnce) {
          failOnce = false
          return {
            status: 503,
            body: { error: { code: 'server_error', message: 'unavailable', retryable: true } },
          }
        }
        const parsed = JSON.parse(req.body) as { body: string }
        const item = aggregate('wrk_one', { ...FIRST, body: parsed.body })
        return { status: 201, body: { data: item, meta: metaOk } }
      },
    })
    renderApp(HOME)
    await fillFields(FIRST)
    const save = await screen.findByRole('button', { name: '保存工作' })
    fireEvent.click(save)
    fireEvent.click(save)
    await screen.findByText(/草稿仍保留/)
    const failedPosts = calls.filter((c) => c.method === 'POST' && c.url === WORK_ITEMS)
    expect(failedPosts).toHaveLength(1)
    const draft = readDraft()
    expect(draft?.body).toBe(FIRST.body)
    expect(draft?.intentKey).toBe(failedPosts[0].idempotencyKey)

    fireEvent.click(screen.getByRole('button', { name: '保存工作' }))
    await screen.findByText('工作已保存')
    const allPosts = calls.filter((c) => c.method === 'POST' && c.url === WORK_ITEMS)
    expect(allPosts).toHaveLength(2)
    expect(allPosts[1].idempotencyKey).toBe(allPosts[0].idempotencyKey)
    expect(calls.some((c) => AGENT_URL.test(c.url))).toBe(false)
  })
})
