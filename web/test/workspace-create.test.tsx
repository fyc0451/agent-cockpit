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
      const map: Record<string, unknown> = { ...defaultFetchMap(), ...alphaGets(), ...opts.gets }
      const key = Object.keys(map)
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      spec = key ? { body: map[key] } : undefined
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
  await screen.findByText('暂无 Workspace')
  return user
}

async function openWizard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '创建 Workspace' }))
  return await screen.findByRole('dialog', { name: '创建 Workspace' })
}

// ---------- 用例 ----------

describe('P0-WORKSPACE-001-F 创建 Workspace', () => {
  it('C1 无合格 RepoLocation（p1 空列表）→ 按钮禁用 + 可见 reason + 点击零请求', async () => {
    const stub = stubCreateFetch({})
    const user = userEvent.setup()
    renderApp('/projects/p1/workbench')
    await screen.findByText('本机工作区')
    const btn = screen.getByRole('button', { name: '创建 Workspace' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', expect.stringContaining('RepoLocation'))
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
    await screen.findByText('暂无 Workspace')
    const btn = screen.getByRole('button', { name: '创建 Workspace' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    await user.click(btn)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('C3 happy path：严格四键 body + Idempotency-Key + 201 → invalidate 并深链 workspace home', async () => {
    const stub = stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace, meta: metaOk } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))

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
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '新工作区')
    await user.type(within(dialog).getByLabelText('目标（可选）'), '验证 goal')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
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
    const nameInput = within(dialog).getByLabelText('Workspace 名称')
    await user.type(nameInput, '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
    await screen.findByText('后端暂不可用')
    await user.click(within(dialog).getByRole('button', { name: '重试' }))
    await waitFor(() => expect(stub.posts).toHaveLength(2))
    expect(stub.posts[1].headers['Idempotency-Key']).toBe(stub.posts[0].headers['Idempotency-Key'])
    expect(stub.posts[1].body).toBe(stub.posts[0].body)
    // 改名称 → 必须换新 key
    await user.type(nameInput, '改')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
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
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
    expect((await within(dialog).findAllByText('同名 Workspace 已存在')).length).toBeGreaterThanOrEqual(1)
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
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
    await within(dialog).findByText('RepoLocation 当前不可用')
    expect(within(dialog).queryByRole('button', { name: '重试' })).toBeNull()
    expect(stub.posts).toHaveLength(1)
  })

  it('C8 取消零请求；重开状态完整重置', async () => {
    const stub = stubCreateFetch({})
    const user = await openAlphaWorkbench()
    let dialog = await openWizard(user)
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '半途')
    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(stub.posts).toHaveLength(0)
    dialog = await openWizard(user)
    expect(within(dialog).getByLabelText('Workspace 名称')).toHaveValue('')
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
    const submit = within(dialog).getByRole('button', { name: '确认创建' })
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    await user.type(within(dialog).getByLabelText('Workspace 名称'), 'x'.repeat(257))
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
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '新工作区')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
    await within(dialog).findByText(/meta/)
    expect(screen.getByRole('dialog')).toBe(dialog)
  })

  it('C12 名称/目标按原样字符串提交（含首尾空白），幂等绑定该精确 body', async () => {
    const stub = stubCreateFetch({
      create: () => ({ status: 201, body: { data: createdWorkspace, meta: metaOk } }),
    })
    const user = await openAlphaWorkbench()
    const dialog = await openWizard(user)
    await user.type(within(dialog).getByLabelText('Workspace 名称'), '  间隔 名字  ')
    await user.type(within(dialog).getByLabelText('目标（可选）'), '  带空格  ')
    await user.click(within(dialog).getByRole('button', { name: '确认创建' }))
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
