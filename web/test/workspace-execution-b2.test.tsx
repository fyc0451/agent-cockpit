import { fireEvent, screen } from '@testing-library/react'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'
import {
  assertWorkspaceExecutionMember,
  assertWorkspacePreparation,
} from '../api/workspaceExecution'

const WORK_ITEMS_URL = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const MEMBERS_URL = `/api/projects/${REG_P1}/workspaces/w1/members`
const PREP_URL = `${WORK_ITEMS_URL}/wrk_b2/preparation`
const HOME_ROUTE = '/projects/p1/workspaces/w1'
const AGENT_API = /(createAgent|sendAgentPrompt|agent-prompt|transcript|\/agents|\/claim|\/reply|pane_send|pane_read)/i
const BODY = '修复登录失败'

const memberAtlas = {
  identity_id: 'idn_atlas',
  display_name: 'Atlas',
  role: 'member',
  lifecycle: 'active',
  revision: 1,
}

function workAggregate() {
  return {
    thread: {
      thread_id: 'thr_b2',
      project_id: REG_P1,
      workspace_id: 'w1',
      revision: 1,
      created_at: '2026-08-16T00:00:00+00:00',
    },
    root_message: {
      message_id: 'msg_b2',
      thread_id: 'thr_b2',
      author_kind: 'boss',
      author_ref: null,
      body: BODY,
    },
    work_item: {
      work_item_id: 'wrk_b2',
      source_message_id: 'msg_b2',
      status: 'unassigned',
      acceptance: null,
      constraints: null,
    },
  }
}

function preparation(over: Record<string, unknown> = {}) {
  return {
    work_item_id: 'wrk_b2',
    state: 'prepared',
    revision: 1,
    work_item_status: 'unassigned',
    identity: memberAtlas,
    principal: { identity_id: 'idn_atlas', generation: 1 },
    checkout: {
      checkout_id: 'chk_b2',
      status: 'ready',
      source_head: 'abc1234',
      source_tree: 'def5678',
      ref_kind: 'detached',
      revision: 1,
    },
    lease: { lease_id: 'les_b2', status: 'reserved', generation: 1, revision: 1 },
    attachment: null,
    ...over,
  }
}

interface RecordedRequest {
  url: string
  method: string
  idempotencyKey: string | null
  body: string
}

function stubB2(opts: {
  members?: unknown[]
  prep?: unknown | null
  prepError?: { status: number; code: string }
  writeError?: { status: number; code: string }
} = {}) {
  let items = [workAggregate()]
  let members = [...(opts.members ?? [])]
  let prep: unknown | null = opts.prep === undefined ? null : opts.prep
  const calls: RecordedRequest[] = []
  const defaults = defaultFetchMap()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const rawHeaders = init?.headers as Record<string, string> | undefined
      const request: RecordedRequest = {
        url,
        method,
        idempotencyKey: rawHeaders?.['Idempotency-Key'] ?? null,
        body: typeof init?.body === 'string' ? init.body : '',
      }
      calls.push(request)
      const ok = (data: unknown, status = 200) => ({
        ok: status >= 200 && status < 300,
        status,
        json: async () => ({ data, meta: metaOk }),
      })
      const err = (status: number, code: string) => ({
        ok: false,
        status,
        json: async () => ({
          error: { code, message: code.replace(/_/g, ' '), retryable: false, request_id: 'req-b2', details: {} },
        }),
      })
      if (url === WORK_ITEMS_URL) {
        if (method === 'POST') return ok(items[0], 201)
        return ok({ items, next_cursor: null })
      }
      if (url === MEMBERS_URL) {
        if (method === 'POST') {
          if (opts.writeError) return err(opts.writeError.status, opts.writeError.code)
          const body = JSON.parse(request.body) as { display_name: string }
          const created = { ...memberAtlas, display_name: body.display_name }
          members = [...members, created]
          return ok(created, 201)
        }
        return ok({ items: members, next_cursor: null })
      }
      if (url === PREP_URL) {
        if (method === 'POST') {
          if (opts.writeError) return err(opts.writeError.status, opts.writeError.code)
          prep = preparation()
          return ok(prep, 201)
        }
        if (opts.prepError) return err(opts.prepError.status, opts.prepError.code)
        if (prep == null) return err(404, 'preparation_not_found')
        return ok(prep)
      }
      if (url === `${PREP_URL}/attach` && method === 'POST') {
        if (opts.writeError) return err(opts.writeError.status, opts.writeError.code)
        prep = preparation({
          state: 'connected_readonly',
          revision: 2,
          attachment: {
            attachment_id: 'att_b2',
            status: 'connected_readonly',
            provider: 'local',
            harness: 'codex',
            generation: 1,
            identity_verified: true,
            revision: 1,
          },
        })
        return ok(prep)
      }
      if (url === `${PREP_URL}/detach` && method === 'POST') {
        prep = preparation({
          state: 'prepared',
          revision: 3,
          attachment: {
            attachment_id: 'att_b2',
            status: 'detached',
            provider: 'local',
            harness: 'codex',
            generation: 1,
            identity_verified: true,
            revision: 2,
          },
        })
        return ok(prep)
      }
      const key = Object.keys(defaults)
        .filter((item) => url === item || url.startsWith(`${item}?`))
        .sort((a, b) => b.length - a.length)[0]
      if (key) {
        return { ok: true, status: 200, json: async () => defaults[key] } as Response
      }
      return err(404, 'not_found') as unknown as Response
    }),
  )
  return calls
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Checkpoint B2 公共 DTO', () => {
  it('成员与 preparation 键集 fail-closed；禁止 path/fence/pane', () => {
    expect(assertWorkspaceExecutionMember(memberAtlas)).toMatchObject(memberAtlas)
    const prepared = preparation()
    expect(assertWorkspacePreparation(prepared).work_item_status).toBe('unassigned')
    expect(() => assertWorkspaceExecutionMember({ ...memberAtlas, identity_id: '' })).toThrow()
    expect(() =>
      assertWorkspacePreparation({ ...prepared, checkout: { ...(prepared.checkout as object), path: '/tmp' } }),
    ).toThrow()
  })
})

describe('Checkpoint B2 执行准备卡', () => {
  it('选中已保存任务后可新建成员、准备、连接、断开；始终未分配/尚未领取', async () => {
    const calls = stubB2()
    renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    expect(await screen.findByText('尚未领取')).toBeInTheDocument()
    expect(screen.getAllByText('未分配').length).toBeGreaterThan(0)
    fireEvent.change(screen.getByLabelText('成员名称'), { target: { value: 'Atlas' } })
    fireEvent.click(screen.getByRole('button', { name: '新建成员' }))
    expect(await screen.findByText('Atlas')).toBeInTheDocument()
    const create = calls.find((call) => call.url === MEMBERS_URL && call.method === 'POST')
    expect(create?.idempotencyKey).toMatch(/./)
    expect(JSON.parse(create?.body ?? '{}')).toEqual({ display_name: 'Atlas' })

    fireEvent.click(screen.getByRole('button', { name: '准备执行' }))
    expect(await screen.findByText(/已准备/)).toBeInTheDocument()
    const prepare = calls.find((call) => call.url === PREP_URL && call.method === 'POST')
    expect(JSON.parse(prepare?.body ?? '{}')).toEqual({ identity_id: 'idn_atlas' })
    expect(prepare?.idempotencyKey).toMatch(/./)

    fireEvent.click(screen.getByRole('button', { name: '连接只读 Agent' }))
    expect(await screen.findByText(/已连接/)).toBeInTheDocument()
    const attach = calls.find((call) => call.url.endsWith('/attach'))
    expect(JSON.parse(attach?.body ?? '{}')).toEqual({ expected_revision: 1 })

    fireEvent.click(screen.getByRole('button', { name: '断开' }))
    expect(await screen.findByRole('button', { name: '连接只读 Agent' })).toBeInTheDocument()
    expect(screen.getByText('尚未领取')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument()
    expect(screen.queryByText(/认领|回复|working|claim|reply/i)).not.toBeInTheDocument()
    expect(calls.some((call) => AGENT_API.test(call.url))).toBe(false)
  })

  it('刷新后从 GET 恢复已准备状态；source_dirty 保留已知态', async () => {
    const first = stubB2({ prep: preparation(), members: [memberAtlas] })
    const mounted = renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    expect(await screen.findByText(/已准备/)).toBeInTheDocument()
    expect(screen.getByText('Atlas')).toBeInTheDocument()
    mounted.unmount()

    vi.unstubAllGlobals()
    const second = stubB2({
      prep: preparation(),
      members: [memberAtlas],
      writeError: { status: 409, code: 'source_dirty' },
    })
    renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    expect(await screen.findByText(/已准备/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '连接只读 Agent' }))
    expect(await screen.findByText(/工作区有未提交更改/)).toBeInTheDocument()
    expect(screen.getByText(/已准备/)).toBeInTheDocument()
    expect(screen.getByText('尚未领取')).toBeInTheDocument()
    expect(first.some((call) => call.url === PREP_URL && call.method === 'GET')).toBe(true)
    expect(second.some((call) => call.url.endsWith('/attach'))).toBe(true)
  })
})
