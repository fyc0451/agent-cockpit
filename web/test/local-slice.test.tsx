import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  assertLegacyEnvCheck,
  assertLegacyHerdrStatus,
  assertLegacyOverview,
  assertLegacyAttention,
  assertLegacySettings,
  assertLegacyTasks,
  assertLegacyWorkbench,
  assertWorkspaceFileContentData,
  assertWorkspaceFileSearchData,
  assertWorkspaceFilesData,
  assertWorkspaceListData,
  assertWorkspaceSummary,
  legacyGet,
  workspaceLocation,
} from '../api/localSlice'
import { ProtocolError } from '../api/client'
import { assertProjectListData } from '../api/registry'
import {
  defaultFetchMap,
  legacyEnvCheckPayload,
  legacyWorkbenchDegradedPayload,
  legacyWorkbenchPayload,
  metaOk,
  REG_P1,
  registryProjectsPayload,
  workspaceDetailW1OpenPayload,
  workspaceW1,
  wsFileContentBinaryPayload,
  wsFileContentPayload,
  wsFilesDegradedPayload,
  wsFilesRootPayload,
  wsFilesSrcPayload,
  wsFileSearchPayload,
} from '../fixtures/api'
import { isSafeRelativePath } from '../pages/FilesPage'
import { renderApp, stubFetch } from './helpers'

const WS_BASE = `/api/project-registry/projects/${REG_P1}/workspaces`

/** files.read 开启的 workspace 世界（detail meta 携带权威 capabilities；按 query 分流 files 载荷） */
function stubOpenFetch(opts: {
  detail?: unknown
  files?: unknown
  srcFiles?: unknown
  content?: unknown
  search?: unknown
} = {}) {
  const map: Record<string, unknown> = {
    ...defaultFetchMap(),
    [`${WS_BASE}/w1`]: opts.detail ?? workspaceDetailW1OpenPayload,
  }
  return stubFetch((url) => {
    const u = new URL(url, 'http://local')
    if (u.pathname === `${WS_BASE}/w1/files`) {
      const p = u.searchParams.get('path') ?? ''
      return { body: p === 'src' ? (opts.srcFiles ?? wsFilesSrcPayload) : (opts.files ?? wsFilesRootPayload) }
    }
    if (u.pathname === `${WS_BASE}/w1/files/content`) {
      return { body: opts.content ?? wsFileContentPayload }
    }
    if (u.pathname === `${WS_BASE}/w1/files/search`) {
      return { body: opts.search ?? wsFileSearchPayload }
    }
    const key = Object.keys(map)
      .filter((k) => url === k || url.startsWith(`${k}?`))
      .sort((a, b) => b.length - a.length)[0]
    return key ? { body: map[key] } : undefined
  })
}

describe('local-slice DTO 守卫（fail-closed）', () => {
  it('WorkspaceSummary：真实 12 键通过；缺键/多键/错型/禁 canonical root 拒绝', () => {
    expect(assertWorkspaceSummary(workspaceW1).workspace_id).toBe('w1')
    expect(workspaceLocation(assertWorkspaceSummary(workspaceW1))).toBe('local')

    const missing = { ...workspaceW1 } as Record<string, unknown>
    delete missing.repo_location
    expect(() => assertWorkspaceSummary(missing)).toThrow(ProtocolError)

    expect(() =>
      assertWorkspaceSummary({ ...workspaceW1, canonical_path: '/repos/p1' }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertWorkspaceSummary({
        ...workspaceW1,
        repo_location: { node_id: 'local', availability: 'available', cwd: '/x' },
      }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertWorkspaceSummary({ ...workspaceW1, version: 0 }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertWorkspaceSummary({
        ...workspaceW1,
        repo_location: { node_id: 'local', availability: 'weird' },
      }),
    ).toThrow(ProtocolError)
  })

  it('WorkspaceListData：精确 {items}；next_cursor 等多键拒绝', () => {
    expect(assertWorkspaceListData({ items: [workspaceW1] }).items).toHaveLength(1)
    expect(() => assertWorkspaceListData({ items: [], next_cursor: null })).toThrow(ProtocolError)
    expect(() => assertWorkspaceListData({})).toThrow(ProtocolError)
  })

  it('tree/content/search 真实形状守卫', () => {
    expect(
      assertWorkspaceFilesData(wsFilesRootPayload.data).entries.map((e) => e.name),
    ).toEqual(['src', 'docs', 'README.md'])
    // entry 带 path/kind（旧占位形状）→ 拒绝
    expect(() =>
      assertWorkspaceFilesData({
        path: '',
        entries: [{ name: 'src', path: 'src', kind: 'directory' }],
      }),
    ).toThrow(ProtocolError)
    // entry size 负数 / type 越界 → 拒绝
    expect(() =>
      assertWorkspaceFilesData({
        path: '',
        entries: [{ name: 'a', type: 'file', size: -1, ext: '' }],
      }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertWorkspaceFilesData({
        path: '',
        entries: [{ name: 'a', type: 'directory', size: 0, ext: '' }],
      }),
    ).toThrow(ProtocolError)

    expect(assertWorkspaceFileContentData(wsFileContentPayload.data).text).toContain('Project One')
    expect(assertWorkspaceFileContentData(wsFileContentBinaryPayload.data).text).toBeUndefined()
    // binary=false 缺 text → 拒绝；旧 content/truncated 形状 → 拒绝
    expect(() =>
      assertWorkspaceFileContentData({ path: 'a.txt', size: 1, binary: false }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertWorkspaceFileContentData({ path: 'a.txt', content: 'x', truncated: false }),
    ).toThrow(ProtocolError)

    expect(assertWorkspaceFileSearchData(wsFileSearchPayload.data).results).toHaveLength(1)
    // 旧 q/items 形状 → 拒绝
    expect(() =>
      assertWorkspaceFileSearchData({ path: '', q: 'main', items: [] }),
    ).toThrow(ProtocolError)
  })

  it('legacy workbench/env-check 严格键集', () => {
    expect(assertLegacyWorkbench(legacyWorkbenchPayload).project.id).toBe(7)
    expect(() =>
      assertLegacyWorkbench({ ...legacyWorkbenchPayload, registry_project_id: REG_P1 }),
    ).toThrow(ProtocolError)
    const missingKey = { ...legacyWorkbenchPayload } as Record<string, unknown>
    delete missingKey.source
    expect(() => assertLegacyWorkbench(missingKey)).toThrow(ProtocolError)
    // G3 envelope 不是裸 legacy → 拒绝
    expect(() => assertLegacyWorkbench({ data: legacyWorkbenchPayload, meta: metaOk })).toThrow(
      ProtocolError,
    )

    expect(assertLegacyEnvCheck(legacyEnvCheckPayload).herdr.installed).toBe(false)
    expect(() =>
      assertLegacyEnvCheck({ ...legacyEnvCheckPayload, python: { installed: true } }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertLegacyEnvCheck({ data: { checks: [] }, meta: metaOk }),
    ).toThrow(ProtocolError)
  })

  it('legacyGet 错误通道：{detail} / 2xx 内嵌 error envelope / 断网', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ detail: 'Agent Mail 不可用' }),
      }) as Response),
    )
    await expect(legacyGet('/api/projects/p1/workbench')).rejects.toMatchObject({
      code: 'server_error',
      message: 'Agent Mail 不可用',
      retryable: true,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ error: { code: 'data_stale', message: '缓存过期', retryable: false } }),
      }) as Response),
    )
    await expect(legacyGet('/api/env-check')).rejects.toMatchObject({ code: 'data_stale' })

    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('boom'))))
    await expect(legacyGet('/api/env-check')).rejects.toMatchObject({ code: 'disconnected' })
  })

  it('isSafeRelativePath：正常相对路径放行，../ 反斜杠 NUL/控制字符拒绝', () => {
    expect(isSafeRelativePath('')).toBe(true)
    expect(isSafeRelativePath('README.md')).toBe(true)
    expect(isSafeRelativePath('docs/guide.txt')).toBe(true)
    expect(isSafeRelativePath('../etc/passwd')).toBe(false)
    expect(isSafeRelativePath('a/../b')).toBe(false)
    expect(isSafeRelativePath('/abs/path')).toBe(false)
    expect(isSafeRelativePath('~/x')).toBe(false)
    expect(isSafeRelativePath('a\\b')).toBe(false)
    expect(isSafeRelativePath('a\0b')).toBe(false)
    expect(isSafeRelativePath('a\nb')).toBe(false)
    expect(isSafeRelativePath('a//b')).toBe(false)
    expect(isSafeRelativePath('a/')).toBe(false)
    expect(isSafeRelativePath('./a')).toBe(false)
  })

  it('Registry list nested 解包 + public location 无 canonical_path', () => {
    const parsed = assertProjectListData(registryProjectsPayload.data)
    const p1 = parsed.items.find((p) => p.slug === 'p1')
    expect(p1?.project_id).toBe(REG_P1)
    const alpha = parsed.items.find((p) => p.slug === 'alpha')
    expect(alpha?.repo_locations?.[0].node_id).toBe('local')
    expect(alpha?.repo_locations?.[0]).not.toHaveProperty('canonical_path')
    // 扁平旧形状 / 带 canonical_path 的 location → 拒绝
    expect(() =>
      assertProjectListData({ items: [{ project_id: 'x', slug: 's' }], next_cursor: null }),
    ).toThrow(ProtocolError)
    expect(() =>
      assertProjectListData({
        items: [
          {
            project: registryProjectsPayload.data.items[0].project,
            repo_locations: [{ ...registryProjectsPayload.data.items[0].repo_locations[0], canonical_path: '/repos/alpha' }],
          },
        ],
        next_cursor: null,
      }),
    ).toThrow(ProtocolError)
  })
})

describe('SLICE-001 页面纵切', () => {
  it('Workbench：真实 assignments/sessions/source + persisted workspaces 深链，无 agents/activity 假设', async () => {
    stubFetch(defaultFetchMap())
    renderApp('/projects/p1/workbench')
    expect(await screen.findByText('修复登录回归')).toBeInTheDocument()
    expect(screen.getByText('main')).toBeInTheDocument() // session 名
    // persisted workspace 深链（rail 也有同名链接，取工作空间面板内的）
    await screen.findByText('工作空间')
    const links = await screen.findAllByRole('link', { name: '本机工作区' })
    expect(
      links.some((l) => l.getAttribute('href')?.includes('/projects/p1/workspaces/w1')),
    ).toBe(true)
    // 旧占位区块不再出现
    expect(screen.queryByText('最近活动')).not.toBeInTheDocument()
  })

  it('Workbench source degraded：sessions 空 → degraded 而非 empty 假态', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/projects/p1/workbench': legacyWorkbenchDegradedPayload,
    })
    const { container } = renderApp('/projects/p1/workbench')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(screen.getByText('会话列表不可用')).toBeInTheDocument()
    expect(screen.queryByText('暂无会话')).not.toBeInTheDocument()
  })

  it('Workspace detail 跨项目/404 → typed error，非 empty', async () => {
    stubFetch(defaultFetchMap())
    const { container } = renderApp('/projects/p1/workspaces/w9')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(screen.getByText('Workspace 不存在或不属于当前项目')).toBeInTheDocument()
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('Workspace detail 串号（project_id 不匹配）→ typed error', async () => {
    stubFetch({
      ...defaultFetchMap(),
      [`${WS_BASE}/w1`]: {
        data: { ...workspaceW1, project_id: 'prj_' + 'ff'.repeat(16) },
        meta: metaOk,
      },
    })
    renderApp('/projects/p1/workspaces/w1')
    expect(await screen.findByText('Workspace 不存在或不属于当前项目')).toBeInTheDocument()
  })

  it('WorkspaceHome：files.read 开 → 文件卡可用链接；terminal.pty 关 → 禁用卡带冻结 reason', async () => {
    stubOpenFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1')
    // 等 workspace home 主体落地（rail 有同名链接，必须锚定主区）
    await screen.findByRole('button', { name: '删除工作空间' })
    const main = container.querySelector('main')!
    const filesCard = within(main).getByRole('link', { name: /文件/ })
    expect(filesCard).toHaveAttribute('href', expect.stringContaining('/projects/p1/workspaces/w1/files'))
    const terminalCard = Array.from(main.querySelectorAll('.card--disabled')).find((c) =>
      c.textContent?.includes('终端'),
    )
    expect(terminalCard).toBeTruthy()
    expect(terminalCard).toHaveAttribute('aria-disabled', 'true')
    expect(terminalCard?.textContent).toContain('workspace_terminal_ticket_deferred')
  })

  it('Files：cap 开 → 目录树/预览/搜索可用，且只发相对 path，无 legacy /api/files 调用', async () => {
    const fetchSpy = stubOpenFetch()
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1/files')
    // 目录树（锚定 files 页工具栏，rail 有同名链接）
    await screen.findByLabelText('搜索文件')
    expect(await screen.findByText('src/')).toBeInTheDocument()
    // 进入子目录（真实 src payload）
    await user.click(screen.getByText('src/'))
    expect(await screen.findByText('main.ts')).toBeInTheDocument()
    // 预览文件
    await user.click(screen.getByText('main.ts'))
    expect(await screen.findByLabelText('文件预览 src/main.ts')).toBeInTheDocument()
    // 搜索
    await user.type(screen.getByLabelText('搜索文件'), 'main')
    await user.click(screen.getByRole('button', { name: '搜索' }))
    expect(await screen.findByLabelText('搜索 main 的结果')).toBeInTheDocument()

    const calls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(calls.filter((u) => u.startsWith('/api/files'))).toEqual([])
    // files 系列请求全部只带相对 path
    const fileCalls = calls.filter((u) => u.includes('/files'))
    expect(fileCalls.length).toBeGreaterThan(0)
    for (const u of fileCalls) {
      const parsed = new URL(u, 'http://local')
      const p = parsed.searchParams.get('path') ?? ''
      expect(isSafeRelativePath(p)).toBe(true)
    }
    // 零 POST
    expect(fetchSpy.mock.calls.every((c) => (c[1]?.method ?? 'GET') === 'GET')).toBe(true)
  })

  it('Files：深链 ?path=&file= 刷新恢复同一 FileRef', async () => {
    stubOpenFetch()
    renderApp('/projects/p1/workspaces/w1/files?path=src&file=README.md')
    expect(await screen.findByText('main.ts')).toBeInTheDocument()
    expect(await screen.findByLabelText('文件预览 README.md')).toBeInTheDocument()
    expect(screen.getByText(/本机只读预览/)).toBeInTheDocument()
  })

  it('Files：非法深链路径 → typed error 且零 files 请求', async () => {
    const fetchSpy = stubOpenFetch()
    renderApp('/projects/p1/workspaces/w1/files?path=../..')
    expect(await screen.findByText('非法路径')).toBeInTheDocument()
    const calls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(calls.filter((u) => u.includes('/workspaces/w1/files'))).toEqual([])
  })

  it('Files：二进制 content → 不可预览态，不渲染文本', async () => {
    stubOpenFetch({ content: wsFileContentBinaryPayload })
    renderApp('/projects/p1/workspaces/w1/files?file=docs/logo.png')
    expect(await screen.findByText('二进制文件不可预览')).toBeInTheDocument()
  })

  it('Files：meta source 降级 → degraded banner，不假 empty', async () => {
    stubOpenFetch({ files: wsFilesDegradedPayload })
    const { container } = renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(screen.getByText('src/')).toBeInTheDocument()
  })

  it('Files：cap 关闭 → forbidden 且零 files 请求（含新 G3 路由）', async () => {
    const fetchSpy = stubFetch(defaultFetchMap())
    renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(screen.getByText('文件浏览暂不可用')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    })
    const calls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(calls.filter((u) => u.includes('/files'))).toEqual([])
  })

  it('Doctor：真实 legacy env-check rows；herdr 失败显示原因而非假成功', async () => {
    stubFetch(defaultFetchMap())
    renderApp('/settings?view=doctor')
    expect(await screen.findByText('codex')).toBeInTheDocument()
    expect(screen.getByText('Herdr 未运行')).toBeInTheDocument()
    expect(screen.getByText('agent_mail')).toBeInTheDocument()
  })

  it('Doctor：env-check 形状违约（G3 envelope）→ typed error 不假空', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/env-check': { data: { checks: [] }, meta: metaOk },
    })
    const { container } = renderApp('/settings?view=doctor')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(screen.getByText(/protocol_error/)).toBeInTheDocument()
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('ProjectScope：slug 不在 Registry 列表 → typed error，不调 legacy /api/projects/{slug}', async () => {
    const fetchSpy = stubFetch(defaultFetchMap())
    renderApp('/projects/ghost/workbench')
    expect(await screen.findByText('项目不存在')).toBeInTheDocument()
    const calls = fetchSpy.mock.calls.map((c) => String(c[0]))
    expect(calls.filter((u) => u === '/api/projects/ghost')).toEqual([])
  })
})

// ===== WEB-004 legacy narrow adapter shape guards =====

describe('WEB-004 legacy shape guards', () => {
  test('assertLegacyHerdrStatus: valid {available, binary}', () => {
    const r = assertLegacyHerdrStatus({ available: true, binary: '/usr/bin/herdr' })
    expect(r.available).toBe(true)
    expect(r.binary).toBe('/usr/bin/herdr')
  })

  test('assertLegacyHerdrStatus: rejects missing key', () => {
    expect(() => assertLegacyHerdrStatus({ available: true })).toThrow(ProtocolError)
    expect(() => assertLegacyHerdrStatus({ binary: 'x' })).toThrow(ProtocolError)
  })

  test('assertLegacyHerdrStatus: rejects wrong type', () => {
    expect(() => assertLegacyHerdrStatus({ available: 'yes', binary: 'x' })).toThrow(ProtocolError)
    expect(() => assertLegacyHerdrStatus(null)).toThrow(ProtocolError)
    expect(() => assertLegacyHerdrStatus([])).toThrow(ProtocolError)
  })

  test('assertLegacyOverview: accepts bare object', () => {
    const r = assertLegacyOverview({ projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: {} })
    expect(r.projects).toEqual([])
  })

  test('assertLegacyOverview: optional project strings accept empty object and reject wrong types', () => {
    const base = { total_unread: 0, total_projects: 1, total_agents: 0, agent_mail: {} }
    expect(assertLegacyOverview({ ...base, projects: [{}] }).projects).toEqual([{}])
    expect(() => assertLegacyOverview({ ...base, projects: [null] })).toThrow(ProtocolError)
    expect(() => assertLegacyOverview({ ...base, projects: [[]] })).toThrow(ProtocolError)
    expect(() => assertLegacyOverview({ ...base, projects: [{ slug: null }] })).toThrow(ProtocolError)
    expect(() => assertLegacyOverview({ ...base, projects: [{ slug: undefined }] })).toThrow(ProtocolError)
    expect(() => assertLegacyOverview({ ...base, projects: [{ name: [] }] })).toThrow(ProtocolError)
    expect(() => assertLegacyOverview({ ...base, projects: [{ branch: 1 }] })).toThrow(ProtocolError)
  })

  test('assertLegacyOverview: rejects non-object', () => {
    expect(() => assertLegacyOverview(null)).toThrow(ProtocolError)
    expect(() => assertLegacyOverview('string')).toThrow(ProtocolError)
    expect(() => assertLegacyOverview([])).toThrow(ProtocolError)
  })

  test('assertLegacyAttention: accepts bare object', () => {
    const r = assertLegacyAttention({ sessions: [], items: [], count: 0, mail_unread: 0, capabilities: {} })
    expect(r.items).toEqual([])
  })

  test('assertLegacyAttention: optional item strings accept empty object and reject wrong types', () => {
    const base = { sessions: [], count: 1, mail_unread: 0, capabilities: {} }
    expect(assertLegacyAttention({ ...base, items: [{}] }).items).toEqual([{}])
    expect(() => assertLegacyAttention({ ...base, items: [null] })).toThrow(ProtocolError)
    expect(() => assertLegacyAttention({ ...base, items: [[]] })).toThrow(ProtocolError)
    expect(() => assertLegacyAttention({ ...base, items: [{ id: 1 }] })).toThrow(ProtocolError)
    expect(() => assertLegacyAttention({ ...base, items: [{ title: {} }] })).toThrow(ProtocolError)
    expect(() => assertLegacyAttention({ ...base, items: [{ title: undefined }] })).toThrow(ProtocolError)
    for (const field of ['kind', 'summary', 'status', 'project', 'workspace', 'created_at', 'url']) {
      expect(() => assertLegacyAttention({ ...base, items: [{ [field]: null }] })).toThrow(ProtocolError)
    }
  })

  test('assertLegacyAttention: rejects non-object', () => {
    expect(() => assertLegacyAttention(42)).toThrow(ProtocolError)
  })

  test('assertLegacySettings: accepts bare object', () => {
    const r = assertLegacySettings({ language: 'zh', known_agents: ['claude'], languages: ['zh', 'en'] })
    expect(r.known_agents).toEqual(['claude'])
  })

  test('assertLegacySettings: rejects non-object', () => {
    expect(() => assertLegacySettings('nope')).toThrow(ProtocolError)
  })

  test('assertLegacyTasks: accepts bare array', () => {
    const r = assertLegacyTasks([])
    expect(r.tasks).toEqual([])
  })

  test('assertLegacyTasks: optional strings accept empty object and reject wrong types', () => {
    expect(assertLegacyTasks([{}]).tasks).toEqual([{}])
    expect(() => assertLegacyTasks([null])).toThrow(ProtocolError)
    expect(() => assertLegacyTasks([[]])).toThrow(ProtocolError)
    expect(() => assertLegacyTasks([{ id: 1 }])).toThrow(ProtocolError)
    expect(() => assertLegacyTasks([{ title: {} }])).toThrow(ProtocolError)
    expect(() => assertLegacyTasks([{ title: undefined }])).toThrow(ProtocolError)
    for (const field of ['status', 'kind', 'project', 'workspace', 'updated_at']) {
      expect(() => assertLegacyTasks([{ [field]: null }])).toThrow(ProtocolError)
    }
  })

  test('assertLegacyTasks: rejects non-object', () => {
    expect(() => assertLegacyTasks(null)).toThrow(ProtocolError)
  expect(() => assertLegacyTasks({})).toThrow(ProtocolError)
  expect(() => assertLegacyTasks('nope')).toThrow(ProtocolError)
  })
})

  test('assertLegacyOverview: rejects array agent_mail', () => {
    expect(() => assertLegacyOverview({ projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: [] })).toThrow(ProtocolError)
  })
  test('assertLegacyOverview: rejects missing total_unread', () => {
    expect(() => assertLegacyOverview({ projects: [], total_projects: 0, total_agents: 0, agent_mail: {} })).toThrow(ProtocolError)
  })
  test('assertLegacyAttention: rejects array capabilities', () => {
    expect(() => assertLegacyAttention({ sessions: [], items: [], count: 0, mail_unread: 0, capabilities: [] })).toThrow(ProtocolError)
  })
  test('assertLegacyAttention: rejects missing items', () => {
    expect(() => assertLegacyAttention({ sessions: [], count: 0, mail_unread: 0, capabilities: {} })).toThrow(ProtocolError)
  })
  test('assertLegacySettings: rejects non-string known_agents', () => {
    expect(() => assertLegacySettings({ language: 'zh', known_agents: [1, 2], languages: ['zh'] })).toThrow(ProtocolError)
  })
  test('assertLegacySettings: rejects missing languages', () => {
    expect(() => assertLegacySettings({ language: 'zh', known_agents: ['claude'] })).toThrow(ProtocolError)
  })
  test('assertLegacyTasks: empty array stays valid', () => {
    const r = assertLegacyTasks([])
    expect(r.tasks).toEqual([])
  })
  test('assertLegacyTasks: rejects object (not array)', () => {
    expect(() => assertLegacyTasks({ tasks: [] })).toThrow(ProtocolError)
  })

  test('assertLegacyOverview: rejects null project element', () => {
    expect(() => assertLegacyOverview({ projects: [null], total_unread: 0, total_projects: 1, total_agents: 0, agent_mail: {} })).toThrow(ProtocolError)
  })
  test('assertLegacyOverview: rejects array project element', () => {
    expect(() => assertLegacyOverview({ projects: [[]], total_unread: 0, total_projects: 1, total_agents: 0, agent_mail: {} })).toThrow(ProtocolError)
  })
  test('assertLegacyAttention: rejects null item element', () => {
    expect(() => assertLegacyAttention({ sessions: [], items: [null], count: 1, mail_unread: 0, capabilities: {} })).toThrow(ProtocolError)
  })
  test('assertLegacyTasks: rejects null element', () => {
    expect(() => assertLegacyTasks([null])).toThrow(ProtocolError)
  })
  test('assertLegacyTasks: rejects numeric id', () => {
    expect(() => assertLegacyTasks([{ id: 1, status: 'running' }])).toThrow(ProtocolError)
  })
  test('assertLegacyTasks: missing optional status stays valid', () => {
    expect(assertLegacyTasks([{ id: 't1' }]).tasks).toEqual([{ id: 't1' }])
  })

describe('Files 行渲染（首用可理解性）', () => {
  it('根目录行 name==fullPath 不重复副行，且仍可点击预览', async () => {
    stubOpenFetch()
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1/files')
    const row = await screen.findByRole('button', { name: 'README.md' })
    // 标题与副行不再重复同一文本
    expect(row.textContent?.match(/README\.md/g)).toHaveLength(1)
    await user.click(row)
    expect(await screen.findByLabelText('文件预览 README.md')).toBeInTheDocument()
  })

  it('搜索提交按钮为 secondary（不抢占主行动）', async () => {
    stubOpenFetch()
    renderApp('/projects/p1/workspaces/w1/files')
    await screen.findByRole('button', { name: 'README.md' })
    const submit = screen.getByRole('button', { name: '搜索' })
    expect(submit).toHaveClass('btn--secondary')
    expect(submit).not.toHaveClass('btn--primary')
  })
})
