import { fireEvent, screen } from '@testing-library/react'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp } from './helpers'
import {
  assertWorkspaceExecutionMember,
  assertWorkspacePreparation,
  getWorkspacePreparation,
  listWorkspaceMembers,
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
  membersError?: { status: number; code: string }
  prep?: unknown | null
  prepError?: { status: number; code: string }
  writeError?: { status: number; code: string }
  dropFirstWrite?: string
} = {}) {
  let items = [workAggregate()]
  let members = [...(opts.members ?? [])]
  let prep: unknown | null = opts.prep === undefined ? null : opts.prep
  let droppedFirstWrite = false
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
      if (method === 'POST' && opts.dropFirstWrite === url && !droppedFirstWrite) {
        droppedFirstWrite = true
        throw new TypeError('Failed to fetch')
      }
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
        if (opts.membersError) return err(opts.membersError.status, opts.membersError.code)
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
            provider: 'local_herdr',
            harness: 'codex_terminal_managed_v1',
            generation: 1,
            identity_verified: true,
            revision: 1,
          },
        })
        return ok(prep)
      }
      if (url === `${PREP_URL}/detach` && method === 'POST') {
        prep = preparation({
          state: 'detached',
          revision: 3,
          attachment: {
            attachment_id: 'att_b2',
            status: 'detached',
            provider: 'local_herdr',
            harness: 'codex_terminal_managed_v1',
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
    const detached = assertWorkspacePreparation(preparation({
      state: 'detached',
      attachment: {
        attachment_id: 'att_b2',
        status: 'detached',
        provider: 'local_herdr',
        harness: 'codex_terminal_managed_v1',
        generation: 1,
        identity_verified: true,
        revision: 2,
      },
    }))
    expect(detached.state).toBe('detached')
    expect(detached.attachment?.provider).toBe('local_herdr')
    expect(detached.attachment?.harness).toBe('codex_terminal_managed_v1')
  })

  it('GET members 的 project/workspace 404 必须抛出，不得降成空列表', async () => {
    const fail = (code: string) => {
      vi.stubGlobal('fetch', vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({
          error: { code, message: code, retryable: false, request_id: 'req-x', details: {} },
        }),
      })))
    }
    fail('project_not_found')
    await expect(listWorkspaceMembers(REG_P1, 'w1')).rejects.toMatchObject({
      code: 'project_not_found',
      status: 404,
    })
    fail('workspace_not_found')
    await expect(listWorkspaceMembers(REG_P1, 'w1')).rejects.toMatchObject({
      code: 'workspace_not_found',
      status: 404,
    })
  })

  it('GET preparation 仅 preparation_not_found 返回 null，其他 404 保留 typed error', async () => {
    const fail = (code: string) => {
      vi.stubGlobal('fetch', vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({
          error: { code, message: code, retryable: false, request_id: 'req-x', details: {} },
        }),
      })))
    }
    fail('preparation_not_found')
    await expect(getWorkspacePreparation(REG_P1, 'w1', 'wrk_b2')).resolves.toMatchObject({ data: null })
    fail('work_item_not_found')
    await expect(getWorkspacePreparation(REG_P1, 'w1', 'wrk_b2')).rejects.toMatchObject({
      code: 'work_item_not_found',
      status: 404,
    })
    fail('project_not_found')
    await expect(getWorkspacePreparation(REG_P1, 'w1', 'wrk_b2')).rejects.toMatchObject({
      code: 'project_not_found',
    })
    fail('workspace_not_found')
    await expect(getWorkspacePreparation(REG_P1, 'w1', 'wrk_b2')).rejects.toMatchObject({
      code: 'workspace_not_found',
    })
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
    expect(calls.some((call) => call.url.endsWith('/detach'))).toBe(true)
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

  it('prepare 首响应丢失后再次点击复用同 endpoint/body/key', async () => {
    const calls = stubB2({ members: [memberAtlas], dropFirstWrite: PREP_URL })
    renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    fireEvent.click(await screen.findByRole('radio', { name: 'Atlas' }))
    fireEvent.click(screen.getByRole('button', { name: '准备执行' }))
    expect(await screen.findByText(/当前无法连接服务/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '准备执行' }))
    expect(await screen.findByText(/已准备/)).toBeInTheDocument()
    const posts = calls.filter((call) => call.url === PREP_URL && call.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(posts[0].idempotencyKey).toMatch(/./)
    expect(posts[1].idempotencyKey).toBe(posts[0].idempotencyKey)
    expect(posts[1].body).toBe(posts[0].body)
    expect(JSON.parse(posts[0].body)).toEqual({ identity_id: 'idn_atlas' })
  })

  it('attach/detach 首响应丢失后再次点击复用同 endpoint/body/key', async () => {
    const attachCalls = stubB2({
      members: [memberAtlas],
      prep: preparation(),
      dropFirstWrite: `${PREP_URL}/attach`,
    })
    const attached = renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    fireEvent.click(await screen.findByRole('button', { name: '连接只读 Agent' }))
    expect(await screen.findByText(/当前无法连接服务/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '连接只读 Agent' }))
    expect(await screen.findByText(/已连接/)).toBeInTheDocument()
    const attachPosts = attachCalls.filter((call) => call.url.endsWith('/attach') && call.method === 'POST')
    expect(attachPosts).toHaveLength(2)
    expect(attachPosts[1].idempotencyKey).toBe(attachPosts[0].idempotencyKey)
    expect(attachPosts[1].body).toBe(attachPosts[0].body)
    expect(JSON.parse(attachPosts[0].body)).toEqual({ expected_revision: 1 })
    attached.unmount()

    vi.unstubAllGlobals()
    const detachCalls = stubB2({
      members: [memberAtlas],
      prep: preparation({
        state: 'connected_readonly',
        revision: 2,
        attachment: {
          attachment_id: 'att_b2',
          status: 'connected_readonly',
          provider: 'local_herdr',
          harness: 'codex_terminal_managed_v1',
          generation: 1,
          identity_verified: true,
          revision: 1,
        },
      }),
      dropFirstWrite: `${PREP_URL}/detach`,
    })
    renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    fireEvent.click(await screen.findByRole('button', { name: '断开' }))
    expect(await screen.findByText(/当前无法连接服务/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '断开' }))
    expect(await screen.findByRole('button', { name: '连接只读 Agent' })).toBeInTheDocument()
    const detachPosts = detachCalls.filter((call) => call.url.endsWith('/detach') && call.method === 'POST')
    expect(detachPosts).toHaveLength(2)
    expect(detachPosts[1].idempotencyKey).toBe(detachPosts[0].idempotencyKey)
    expect(detachPosts[1].body).toBe(detachPosts[0].body)
    expect(JSON.parse(detachPosts[0].body)).toEqual({ expected_revision: 2 })
  })

  it('换成员后 prepare 换新 key；加载 scope 404 不展示可写动作', async () => {
    const memberBob = { ...memberAtlas, identity_id: 'idn_bob', display_name: 'Bob' }
    const calls = stubB2({ members: [memberAtlas, memberBob], dropFirstWrite: PREP_URL })
    const first = renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    fireEvent.click(await screen.findByRole('radio', { name: 'Atlas' }))
    fireEvent.click(screen.getByRole('button', { name: '准备执行' }))
    expect(await screen.findByText(/当前无法连接服务/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Bob' }))
    fireEvent.click(screen.getByRole('button', { name: '准备执行' }))
    expect(await screen.findByText(/已准备/)).toBeInTheDocument()
    const posts = calls.filter((call) => call.url === PREP_URL && call.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(JSON.parse(posts[0].body)).toEqual({ identity_id: 'idn_atlas' })
    expect(JSON.parse(posts[1].body)).toEqual({ identity_id: 'idn_bob' })
    expect(posts[1].idempotencyKey).not.toBe(posts[0].idempotencyKey)
    first.unmount()

    vi.unstubAllGlobals()
    stubB2({ membersError: { status: 404, code: 'project_not_found' } })
    const missingProject = renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    expect(await screen.findByText(/无法完整读取执行准备/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建成员' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '准备执行' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('成员名称')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '连接只读 Agent' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '断开' })).not.toBeInTheDocument()
    expect(screen.getByText('尚未领取')).toBeInTheDocument()
    missingProject.unmount()

    vi.unstubAllGlobals()
    stubB2({
      members: [memberAtlas],
      prepError: { status: 404, code: 'work_item_not_found' },
    })
    renderApp(`${HOME_ROUTE}?work=wrk_b2`)
    expect(await screen.findByText(/无法完整读取执行准备/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '新建成员' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '准备执行' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '连接只读 Agent' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '断开' })).not.toBeInTheDocument()
  })
})
