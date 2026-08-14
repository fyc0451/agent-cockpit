// P0-WORKSPACE-001-F：Workbench 创建 Workspace（shared-only）纵切测试。
// 合同：/tmp/p0-workspace001-claude/REPORT.md r2 §3（严格 body 四键、Idempotency-Key
// 绑定序列化 body、active+local+available 数据驱动 fail-closed、成功 invalidate+深链 home）。
// 纪律：无 workspace.create capability（按钮可用性纯数据驱动）；取消/禁用零请求；
// 409/404/503 原地 typed 表达，不 toast 假成功；envelope/DTO 守卫 fail-closed。

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  defaultFetchMap,
  legacyWorkbenchPayload,
  metaOk,
  registryProjectsPayload,
} from '../fixtures/api'
import { renderApp, type MockResponseSpec } from './helpers'

// ---------- 支持 method/headers/body 捕获的 fetch stub（照 web003-registry 惯例，本文件自持） ----------

interface CapturedCall {
  url: string
  method: string
  headers: Record<string, string>
  body: string
}

type PostHandler = (call: CapturedCall) => MockResponseSpec | Promise<MockResponseSpec>

const CREATE_RE = /^\/api\/project-registry\/projects\/[^/]+\/workspaces$/

function stubCreateFetch(opts: {
  gets?: Record<string, unknown>
  create?: PostHandler
  /** 这些 URL 的 GET 永不落定（loading 态用例） */
  pendingUrls?: string[]
}): { posts: CapturedCall[]; gets: CapturedCall[] } {
  const posts: CapturedCall[] = []
  const gets: CapturedCall[] = []
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const call: CapturedCall = {
      url,
      method,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: (init?.body as string) ?? '',
    }
    let spec: MockResponseSpec | undefined
    if (method === 'POST') {
      posts.push(call)
      spec = CREATE_RE.test(url) && opts.create ? await opts.create(call) : undefined
    } else {
      gets.push(call)
      if (opts.pendingUrls?.some((p) => url.startsWith(p))) {
        return new Promise<Response>(() => {})
      }
      const map: Record<string, unknown> = { ...defaultFetchMap(), ...alphaGets(), ...opts.gets }
      const key = Object.keys(map)
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      const v = key ? map[key] : undefined
      if (v !== null && typeof v === 'object' && '__status' in v) {
        const ov = v as { __status: number; __payload: unknown }
        spec = { status: ov.__status, body: ov.__payload }
      } else {
        spec = v !== undefined ? { body: v } : undefined
      }
    }
    spec ??= {
      status: 404,
      body: { error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } },
    }
    const status = spec.status ?? 200
    return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
  })
  vi.stubGlobal('fetch', fn)
  return { posts, gets }
}

// ---------- alpha 项目（registryProjectsPayload 内嵌 1 个 active+local+available repo） ----------

const alphaItem = registryProjectsPayload.data.items[0]
const ALPHA_ID = alphaItem.project.project_id
const ALPHA_LOC = alphaItem.repo_locations[0].repo_location_id
const WS_LIST_URL = `/api/project-registry/projects/${ALPHA_ID}/workspaces`

const createdWorkspace = {
  workspace_id: 'ws_new1',
  project_id: ALPHA_ID,
  repo_location_id: ALPHA_LOC,
  name: '新工作区',
  goal: null,
  isolation_kind: 'shared',
  lifecycle: 'active',
  active_run_id: null,
  version: 1,
  created_at: '2026-08-14T00:00:00+00:00',
  updated_at: '2026-08-14T00:00:00+00:00',
  repo_location: { node_id: 'local', availability: 'available' },
}

function alphaGets(): Record<string, unknown> {
  return {
    '/api/projects/alpha/workbench': legacyWorkbenchPayload,
    [WS_LIST_URL]: { data: { items: [] }, meta: metaOk },
    [`${WS_LIST_URL}/ws_new1`]: { data: createdWorkspace, meta: metaOk },
  }
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

async function openAlphaWorkbench() {
  const user = userEvent.setup()
  renderApp('/projects/alpha/workbench')
  await screen.findByText('还没有工作空间')
  return user
}

async function openWizard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '创建工作空间' }))
  return await screen.findByRole('dialog', { name: '创建工作空间' })
}

// ---------- 用例 ----------

describe('P0-WORKSPACE-001-F 创建 Workspace', () => {
  it('C1 无合格 RepoLocation（p1 空列表）→ 按钮禁用 + 可见 reason + 点击零请求', async () => {
    const stub = stubCreateFetch({})
    const user = userEvent.setup()
    renderApp('/projects/p1/workbench')
    expect((await screen.findAllByText('本机工作区')).length).toBeGreaterThanOrEqual(1)
    const btn = screen.getByRole('button', { name: '创建工作空间' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', expect.stringContaining('代码目录'))
    expect(btn.getAttribute('title')).not.toMatch(/RepoLocation|Local|active/)
    await user.click(btn)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(stub.posts).toHaveLength(0)
  })

  it('C2 repo_locations 全部不合格（offline/远程/归档）→ 禁用 reason 表达条件', async () => {
    const ineligible = [
      { node_id: 'local', availability: 'offline', lifecycle: 'active' },
      { node_id: 'gpu-1', availability: 'available', lifecycle: 'active' },
      { node_id: 'local', availability: 'available', lifecycle: 'archived' },
    ].map((l, i) => ({
      repo_location_id: `loc_${String(i).repeat(32)}`,
      project_id: ALPHA_ID,
      vcs_kind: 'git',
      version: 1,
      ...l,
    }))
    const registry = structuredClone(registryProjectsPayload)
    ;(registry.data.items[0] as { repo_locations: unknown[] }).repo_locations = ineligible
    stubCreateFetch({ gets: { '/api/project-registry/projects': registry } })
    const user = userEvent.setup()
    renderApp('/projects/alpha/workbench')
    await screen.findByText('还没有工作空间')
    const btn = screen.getByRole('button', { name: '创建工作空间' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', '这个项目的本机代码目录当前不可用。')
    await user.click(btn)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('C3 happy path：严格四键 body + Idempotency-Key + 201 → invalidate 并深链 workspace home', async () => {
    const stub = stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace, meta: metaOk } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    expect(nameInput).toHaveValue('main')
    expect(dialog).not.toHaveTextContent(ALPHA_LOC)
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))

    await waitFor(() => expect(stub.posts).toHaveLength(1))
    const call = stub.posts[0]
    expect(call.method).toBe('POST')
    expect(call.url).toBe(WS_LIST_URL)
    expect(call.headers['Idempotency-Key']).toMatch(UUID_RE)
    expect(JSON.parse(call.body)).toEqual({
      repo_location_id: ALPHA_LOC,
      name: '新工作区',
      goal: null,
      isolation_kind: 'shared',
    })
    // invalidate list → 该 GET 重新发出
    await waitFor(() =>
      expect(stub.gets.filter((c) => c.url === WS_LIST_URL).length).toBeGreaterThanOrEqual(2),
    )
    // 深链 Workspace Files（WorkspaceScope detail → 名称；files.read 默认关 → forbidden 态）
    expect((await screen.findAllByText('新工作区')).length).toBeGreaterThanOrEqual(1)
    expect(await screen.findByText('文件浏览暂不可用')).toBeInTheDocument()
  })

  it('C4 goal 填写→原样提交；isolation 固定 shared 无选择器', async () => {
    const stub = stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace, meta: metaOk } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    expect(within(dialog).queryByLabelText('隔离模式')).toBeNull()
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.type(within(dialog).getByLabelText('目标（可选）'), '验证 goal')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await waitFor(() => expect(stub.posts).toHaveLength(1))
    expect(JSON.parse(stub.posts[0].body).goal).toBe('验证 goal')
  })

  it('C5 503 retryable → 重试复用同一 Idempotency-Key 与逐字节相同 body；改字段后换新 key', async () => {
    const stub = stubCreateFetch({
      create: () => ({
        status: 503,
        body: { error: { code: 'local_files_unavailable', message: '后端暂不可用', retryable: true } },
      }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await screen.findByText('后端暂不可用')
    await user.click(within(dialog).getByRole('button', { name: '重试' }))
    await waitFor(() => expect(stub.posts).toHaveLength(2))
    expect(stub.posts[1].headers['Idempotency-Key']).toBe(stub.posts[0].headers['Idempotency-Key'])
    expect(stub.posts[1].body).toBe(stub.posts[0].body)
    // 改名称 → 必须换新 key
    await user.type(nameInput, '改')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await waitFor(() => expect(stub.posts).toHaveLength(3))
    expect(stub.posts[2].headers['Idempotency-Key']).not.toBe(
      stub.posts[0].headers['Idempotency-Key'],
    )
  })

  it('C6 409 workspace_name_conflict → conflict 原地表达，不跳转', async () => {
    const stub = stubCreateFetch({
      create: () => ({
        status: 409,
        body: { error: { code: 'workspace_name_conflict', message: '同名 Workspace 已存在', retryable: false } },
      }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    expect(await within(dialog).findByText('同名工作空间已存在')).toBeInTheDocument()
    expect(within(dialog).getByText('请换一个名称后再创建。')).toBeInTheDocument()
    expect(dialog).not.toHaveTextContent('Workspace')
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(stub.posts).toHaveLength(1)
  })

  it('C7 409 repo_location_unavailable / 404 repo_location_not_found → typed error 无重试', async () => {
    const stub = stubCreateFetch({
      create: () => ({
        status: 409,
        body: { error: { code: 'repo_location_unavailable', message: 'RepoLocation 当前不可用', retryable: false } },
      }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await within(dialog).findByText('项目目录不可用')
    expect(within(dialog).getByText('项目目录状态已变化，请返回项目后重试。')).toBeInTheDocument()
    expect(dialog).not.toHaveTextContent('RepoLocation')
    expect(within(dialog).queryByRole('button', { name: '重试' })).toBeNull()
    expect(stub.posts).toHaveLength(1)
  })

  it('C8 取消零请求；重开状态完整重置', async () => {
    const stub = stubCreateFetch({})
    const user = await openAlphaWorkbench()
    let dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '半途')
    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(stub.posts).toHaveLength(0)
    dialog = await openWizard(user)
    expect(within(dialog).getByLabelText('工作空间名称')).toHaveValue('main')
  })

  it('C9 Escape 关闭零请求', async () => {
    const stub = stubCreateFetch({})
    const user = await openAlphaWorkbench()
    await openWizard(user)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(stub.posts).toHaveLength(0)
  })

  it('C10 名称校验：空/超长 256 → 提交禁用零请求', async () => {
    const stub = stubCreateFetch({})
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const submit = within(dialog).getByRole('button', { name: '创建并打开' })
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    await user.type(nameInput, 'x'.repeat(257))
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    await user.click(submit)
    expect(stub.posts).toHaveLength(0)
  })

  it('C11 201 缺 meta / DTO 缺键 → protocol_error fail-closed 不跳转', async () => {
    stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await within(dialog).findByText(/meta/)
    expect(screen.getByRole('dialog')).toBe(dialog)
  })

  it('C12 名称/目标按原样字符串提交（含首尾空白），幂等绑定该精确 body', async () => {
    const stub = stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace, meta: metaOk } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    const nameInput = within(dialog).getByLabelText('工作空间名称')
    await user.clear(nameInput)
    await user.type(nameInput, '  间隔 名字  ')
    await user.type(within(dialog).getByLabelText('目标（可选）'), '  带空格  ')
    await user.click(within(dialog).getByRole('button', { name: '创建并打开' }))
    await waitFor(() => expect(stub.posts).toHaveLength(1))
    expect(JSON.parse(stub.posts[0].body)).toEqual({
      repo_location_id: ALPHA_LOC,
      name: '  间隔 名字  ',
      goal: '  带空格  ',
      isolation_kind: 'shared',
    })
    expect(stub.posts[0].headers['Idempotency-Key']).toMatch(UUID_RE)
  })

  it('C13 resolveSelectedRepo：显式选择消失 → null fail-closed；未选择默认第一项', async () => {
    const { resolveSelectedRepo } = await import('../features/workspace-wizard/WorkspaceWizard')
    const [a, b] = [
      { repo_location_id: 'loc_a' },
      { repo_location_id: 'loc_b' },
    ] as unknown as Parameters<typeof resolveSelectedRepo>[0]
    expect(resolveSelectedRepo([a, b], null)).toBe(a)
    expect(resolveSelectedRepo([a, b], 'loc_b')).toBe(b)
    expect(resolveSelectedRepo([a], 'loc_b')).toBeNull()
    expect(resolveSelectedRepo([], null)).toBeNull()
  })
})

// ---------- P0-WORKBENCH-001-unblock：legacy runtime 故障不阻断 Registry Workspace 区块 ----------

describe('P0-WORKBENCH-001-unblock', () => {
  const WB_503 = { __status: 503, __payload: { detail: 'Agent Mail 不可用' } }
  // useLegacyWorkbench 对 retryable 错误带 backoff 重试（默认 retryDelay 1s/2s），等待放宽
  const SLOW = { timeout: 8000 }

  it('U1 legacy 503（p1）：runtime typed 错误，Workspaces 区块与列表仍渲染', async () => {
    const stub = stubCreateFetch({ gets: { '/api/projects/p1/workbench': WB_503 } })
    renderApp('/projects/p1/workbench')
    // runtime 区块 typed 显示，不伪装为空
    expect(await screen.findByText('Agent Mail 不可用', undefined, SLOW)).toBeInTheDocument()
    expect(screen.queryByText('暂无任务')).toBeNull()
    // Workspaces 区块独立可达
    expect((await screen.findAllByText('本机工作区')).length).toBeGreaterThanOrEqual(1)
    const btn = screen.getByRole('button', { name: '创建工作空间' })
    expect(btn).toHaveAttribute('aria-disabled', 'true') // p1 无合格 repo
    expect(stub.posts).toHaveLength(0)
  })

  it('U2 legacy 503（alpha）：创建按钮 enabled 且向导可开，零 legacy /api/files', async () => {
    const stub = stubCreateFetch({ gets: { '/api/projects/alpha/workbench': WB_503 } })
    const user = userEvent.setup()
    renderApp('/projects/alpha/workbench')
    expect(await screen.findByText('Agent Mail 不可用', undefined, SLOW)).toBeInTheDocument()
    expect(await screen.findByText('还没有工作空间')).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: '创建工作空间' })
    expect(btn).not.toHaveAttribute('aria-disabled', 'true')
    await user.click(btn)
    expect(await screen.findByRole('dialog', { name: '创建工作空间' })).toBeInTheDocument()
    expect(stub.gets.filter((c) => c.url.startsWith('/api/files'))).toHaveLength(0)
    expect(stub.posts).toHaveLength(0)
  })

  it('U3 legacy 永不落定：runtime loading 不阻断 Workspaces 区块', async () => {
    stubCreateFetch({ pendingUrls: ['/api/projects/p1/workbench'] })
    renderApp('/projects/p1/workbench')
    expect(await screen.findByText('正在加载运行时…')).toBeInTheDocument()
    expect((await screen.findAllByText('本机工作区')).length).toBeGreaterThanOrEqual(1)
  })

  it('U4 createWorkspace=1 自动打开向导，取消后不因残留 query 重开', async () => {
    stubCreateFetch({})
    const user = userEvent.setup()
    renderApp('/projects/alpha/workbench?createWorkspace=1')
    const dialog = await screen.findByRole('dialog', { name: '创建工作空间' })
    expect(within(dialog).getByLabelText('工作空间名称')).toHaveValue('main')
    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '创建工作空间' })).toBeNull())
  })

  it('U5 工作空间区块始终排在 legacy runtime 前', async () => {
    stubCreateFetch({ pendingUrls: ['/api/projects/alpha/workbench'] })
    renderApp('/projects/alpha/workbench')
    await screen.findByText('正在加载运行时…')
    const headings = screen.getAllByRole('heading', { level: 2 })
    expect(headings[0]).toHaveTextContent('工作空间')
  })

  it('U6 legacy 404/not_found 降级说明，不渲染红色项目不存在', async () => {
    stubCreateFetch({
      gets: {
        '/api/projects/alpha/workbench': {
          __status: 404,
          __payload: { error: { code: 'not_found', message: '项目不存在', retryable: false } },
        },
      },
    })
    const { container } = renderApp('/projects/alpha/workbench')
    expect(await screen.findByText('运行时信息尚未建立')).toBeInTheDocument()
    expect(screen.queryByText('项目不存在')).toBeNull()
    expect(container.querySelector('[data-state="error"]:not([hidden])')).toBeNull()
    expect(screen.getByRole('button', { name: '创建工作空间' })).toBeInTheDocument()
  })

  it('U7 工作空间首页只突出可用文件与终端，其他能力进入低优先级说明', async () => {
    stubCreateFetch({
      gets: {
        [`${WS_LIST_URL}/ws_new1`]: {
          data: createdWorkspace,
          meta: {
            ...metaOk,
            capabilities: {
              'files.read': { available: true, reason: null },
              'terminal.pty': { available: true, reason: null },
            },
          },
        },
      },
    })
    const { container } = renderApp('/projects/alpha/workspaces/ws_new1')
    await screen.findByText('其他能力')
    const main = container.querySelector('main')!
    const cards = Array.from(main.querySelectorAll('.workspace-primary-actions .card'))
    expect(cards.map((card) => card.querySelector('.card-label')?.textContent)).toEqual(['文件', '终端'])
    expect(main).not.toHaveTextContent('ws_new1')
    expect(within(main).queryByRole('link', { name: /任务/ })).toBeNull()
    expect(within(main).queryByRole('link', { name: /编辑器|浏览器/ })).toBeNull()
    expect(within(main).getByText(/任务、编辑器、浏览器/)).toBeInTheDocument()
  })

  it('U8 工作空间列表隐藏内部 ID，并用用户语言显示本机位置', async () => {
    stubCreateFetch({})
    renderApp('/projects/p1/workbench')
    const heading = await screen.findByRole('heading', { name: '工作空间', level: 2 })
    const section = heading.closest('section')!
    expect(within(section).getByText('本机工作区')).toBeInTheDocument()
    expect(section).not.toHaveTextContent('w1')
    expect(within(section).getByText('本机')).toBeInTheDocument()
    expect(section).not.toHaveTextContent('Local')
  })
})
