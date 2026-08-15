import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProtocolError } from '../api/client'
import { assertDirectoryListingData, assertDiscoveryResultData, assertRootsData } from '../api/registry'
import {
  defaultFetchMap,
  directoriesPartialPayload,
  directoriesPayload,
  discoveryDegradedPayload,
  discoveryExactMatchPayload,
  discoveryGitPayload,
  discoveryPlainPayload,
  discoveryPossiblePayload,
  metaOk,
  registerCreatedPayload,
  registryProjectsEmptyPayload,
  registryProjectsPayload,
  rootsPayload,
  runtimeNodesMultiUsablePayload,
} from '../fixtures/api'
import { renderApp, stubFetch, type MockResponseSpec } from './helpers'

// ---------- 支持 method/headers/body 捕获的 fetch stub（helpers.stubFetch 只传 url，本文件需要 POST 断言） ----------

interface CapturedCall {
  url: string
  method: string
  headers: Record<string, string>
  body: string
}

interface WizardStub {
  posts: CapturedCall[]
  gets: CapturedCall[]
}

type PostHandler = (call: CapturedCall) => MockResponseSpec | Promise<MockResponseSpec>

function stubWizardFetch(opts: {
  gets?: Record<string, unknown>
  discovery?: PostHandler
  register?: PostHandler
}): WizardStub {
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
      const h = url.startsWith('/api/project-discovery')
        ? opts.discovery
        : url.startsWith('/api/project-registry/projects')
          ? opts.register
          : undefined
      spec = h ? await h(call) : undefined
    } else {
      gets.push(call)
      // 项目列表保持只读；Wizard 写权限只能由本次 discovery response 自举。
      const map: Record<string, unknown> = {
        ...defaultFetchMap(),
        '/api/project-registry/projects': registryProjectsPayload,
        ...opts.gets,
      }
      const key = Object.keys(map)
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      spec = key ? { body: map[key] } : undefined
    }
    spec ??= { status: 404, body: { error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } } }
    const status = spec.status ?? 200
    return { ok: status >= 200 && status < 300, status, json: async () => spec.body } as Response
  })
  vi.stubGlobal('fetch', fn)
  return { posts, gets }
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const ROOT_ID_RE = /^root_[0-9a-f]{24}$/

// ---------- 驱动到各步骤的公共流程 ----------

async function openWizard(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '添加项目' }))
  return screen.findByRole('dialog', { name: '添加项目' })
}

async function toDirStep(user: ReturnType<typeof userEvent.setup>) {
  await openWizard(user)
  // 默认 fixture 唯一可用 local 节点 → 自动跳过位置步
  await user.click(await screen.findByRole('button', { name: '代码' }))
  await screen.findByRole('button', { name: /^alpha/ })
}

async function toProbeResult(user: ReturnType<typeof userEvent.setup>, dirName = 'alpha') {
  await toDirStep(user)
  await user.click(screen.getByRole('button', { name: new RegExp(`^${dirName}`) }))
  await user.click(screen.getByRole('button', { name: '检查并继续' }))
}

const discoveryOk = () => ({ body: discoveryGitPayload })
const registerOk = () => ({ body: registerCreatedPayload })

describe('WEB-005 roots 与 discovery capability bootstrap', () => {
  it('roots 只接受 data.items，旧 data.roots fail-closed', () => {
    expect(assertRootsData(rootsPayload.data)).toEqual(rootsPayload.data)
    expect(() => assertRootsData({ roots: rootsPayload.data.items })).toThrow(ProtocolError)
  })
})

describe('WEB-003 列表（V1–V6）', () => {
  it('V1 列表 ready：display_name + availability tag + canonical_path，无 branch/remote 文本', async () => {
    stubFetch(defaultFetchMap())
    const { container } = renderApp('/projects')
    expect(await screen.findByText('Alpha 项目')).toBeInTheDocument()
    expect(screen.getByText('Beta 项目')).toBeInTheDocument()
    expect(screen.getByText('可用')).toBeInTheDocument()
    expect(screen.getByText('离线')).toBeInTheDocument()
    // SLICE-001：行只渲染用户可理解的位置，无 canonical_path / 内部 node_id
    expect(screen.getAllByText('本机')).toHaveLength(2)
    expect(container.querySelector('.list')?.textContent).not.toContain('Local')
    expect(container.querySelector('.list')?.textContent).not.toContain('/repos')
    // 不再有 branch/remote/workspaces 计数等旧字段文本
    expect(container.querySelector('.list')?.textContent).not.toContain('main')
    expect(container.querySelector('.list')?.textContent).not.toContain('workspaces')
  })

  it('V2 列表 empty：data-state=empty + 「选择代码目录」CTA', async () => {
    stubFetch({ ...defaultFetchMap(), '/api/project-registry/projects': registryProjectsEmptyPayload })
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="empty"]')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '选择代码目录' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '查看引导' })).toBeNull()
    expect(container).not.toHaveTextContent('Project')
  })

  it('V3 列表 degraded：partial=true → banner，无 empty 无 0 计数', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/project-registry/projects': {
        data: registryProjectsPayload.data,
        meta: { ...metaOk, partial: true },
      },
    })
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument())
    expect(screen.getByText('Alpha 项目')).toBeInTheDocument()
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/共 0|0 个项目/)
  })

  it('V4 列表 503（retryable）：typed error + 重试按钮，无 empty', async () => {    // 503 envelope retryable → hook 退避重试 ~3s
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/project-registry/projects')) {
          return {
            ok: false,
            status: 503,
            json: async () => ({ error: { code: 'server_error', message: 'registry 暂不可用', retryable: true } }),
          } as Response
        }
        const map = defaultFetchMap()
        const key = Object.keys(map).filter((k) => url === k || url.startsWith(`${k}?`)).sort((a, b) => b.length - a.length)[0]
        const body = key ? map[key] : { error: { code: 'not_found', message: 'no mock', retryable: false } }
        const status = key ? 200 : 404
        return { ok: !!key, status, json: async () => body } as Response
      }),
    )
    const { container } = renderApp('/projects')
    expect(await screen.findByRole('button', { name: '重试' }, { timeout: 9000 })).toBeInTheDocument()
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('V5 列表裸 dict（非 envelope）→ ProtocolError typed error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/project-registry/projects')) {
          return { ok: true, status: 200, json: async () => ({ items: [] }) } as Response
        }
        const map = defaultFetchMap()
        const key = Object.keys(map).filter((k) => url === k || url.startsWith(`${k}?`)).sort((a, b) => b.length - a.length)[0]
        const body = key ? map[key] : { error: { code: 'not_found', message: 'no mock', retryable: false } }
        return { ok: !!key, status: key ? 200 : 404, json: async () => body } as Response
      }),
    )
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="error"]')).toBeInTheDocument())
    expect(screen.getByText(/错误码：protocol_error/)).toBeInTheDocument()
  })

  it('V6 next_cursor 非 null → degraded 提示，且不发出带 cursor 的请求', async () => {
    const stub = stubWizardFetch({
      gets: {
        '/api/project-registry/projects': {
          data: { ...registryProjectsPayload.data, next_cursor: 'cur_next' },
          meta: metaOk,
        },
      },
    })
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument())
    expect(screen.getByText('Alpha 项目')).toBeInTheDocument()
    expect(stub.gets.filter((c) => c.url.includes('cursor='))).toEqual([])
  })

  // 以下两例承接旧 query-error.test.tsx 的列表错误态覆盖（旧用例 stub /api/overview，
  // ProjectsPage 数据源已切换到 registry endpoint，等效断言按新数据源在此保留）
  it('列表 403 forbidden：无重试按钮，显示 code/message/request_id + docs 入口', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/project-registry/projects')) {
          return {
            ok: false,
            status: 403,
            json: async () => ({
              error: { code: 'forbidden', message: '没有访问权限', retryable: false, request_id: 'req-f1' },
            }),
          } as Response
        }
        const map = defaultFetchMap()
        const key = Object.keys(map).filter((k) => url === k || url.startsWith(`${k}?`)).sort((a, b) => b.length - a.length)[0]
        const body = key ? map[key] : { error: { code: 'not_found', message: 'no mock', retryable: false } }
        return { ok: !!key, status: key ? 200 : 404, json: async () => body } as Response
      }),
    )
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    expect(screen.getByText('没有访问权限')).toBeInTheDocument()
    expect(screen.getByText(/request_id: req-f1/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看路线图' })).toBeInTheDocument()
  })

  it('列表 409 conflict：无重试按钮（conflict 预留分支）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.startsWith('/api/project-registry/projects')) {
          return {
            ok: false,
            status: 409,
            json: async () => ({ error: { code: 'conflict', message: '版本冲突', retryable: false } }),
          } as Response
        }
        const map = defaultFetchMap()
        const key = Object.keys(map).filter((k) => url === k || url.startsWith(`${k}?`)).sort((a, b) => b.length - a.length)[0]
        const body = key ? map[key] : { error: { code: 'not_found', message: 'no mock', retryable: false } }
        return { ok: !!key, status: key ? 200 : 404, json: async () => body } as Response
      }),
    )
    const { container } = renderApp('/projects')
    await waitFor(() => expect(container.querySelector('[data-state="conflict"]')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })
})

describe('WEB-003 向导浏览与识别（V7–V13, V17, V20）', () => {
  it('V7 roots 按 display_name 序渲染，root_id 形状合法', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    // 唯一可用 local 节点 → 自动跳到代码位置列表
    const code = await screen.findByRole('button', { name: '代码' })
    const docs = screen.getByRole('button', { name: '文档' })
    expect(code.compareDocumentPosition(docs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    for (const el of [code, docs]) {
      const id = el.getAttribute('data-root-id') ?? ''
      expect(id).toMatch(ROOT_ID_RE)
    }
  })

  it('V8 目录行 tag：git → Git，registered_project → 已登记；页面无绝对路径文本', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toDirStep(user)
    const alpha = screen.getByRole('button', { name: /^alpha/ })
    expect(alpha.textContent).toContain('Git')
    const beta = screen.getByRole('button', { name: /^beta/ })
    expect(beta.textContent).toContain('已登记')
    // 向导内不得泄漏绝对路径（列表页的 canonical_path 是 registry 冻结字段，不在此范围）
    const dialog = screen.getByRole('dialog', { name: '添加项目' })
    expect(dialog.textContent).not.toContain('/repos')
    // 进入子目录的请求只带 root_id + 相对 path
    await user.click(screen.getByRole('button', { name: '进入 alpha' }))
    await waitFor(() => {
      const dirCalls = stub.gets.filter((c) => c.url.includes('/directories'))
      expect(dirCalls.some((c) => c.url.includes('path=alpha'))).toBe(true)
      expect(dirCalls.every((c) => !c.url.includes('/repos'))).toBe(true)
    })
  })

  it('代码位置本身不可选择，只允许从其子目录开始登记', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toDirStep(user)

    expect(screen.queryByText('当前目录')).toBeNull()
    expect(screen.queryByRole('button', { name: /选择当前目录/ })).toBeNull()
    expect(screen.getByRole('button', { name: '检查并继续' })).toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('button', { name: '进入 alpha' }))
    await user.click(await screen.findByRole('button', { name: '上级目录' }))
    expect(await screen.findByRole('button', { name: /^alpha/ })).toBeInTheDocument()
    expect(stub.posts).toHaveLength(0)
  })

  it('V9 discovery complete=true git → NEW_GIT，提交按钮可用；分支只布尔表达', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    const submit = screen.getByRole('button', { name: '确认添加' })
    expect(submit).not.toHaveAttribute('aria-disabled')
    // B1：branch_present 布尔表达；不得出现 raw branch/upstream 名
    const dialog = screen.getByRole('dialog', { name: '添加项目' })
    expect(dialog.textContent).toContain('存在分支')
    expect(dialog.textContent).not.toContain('main')
  })

  it('V10 discovery kind=none → PLAIN_DIR warning + Git CTA disabled + reason', async () => {
    stubWizardFetch({ discovery: () => ({ body: discoveryPlainPayload }), register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user, 'beta')
    await screen.findByText('普通目录')
    const gitCta = screen.getByText(/该目录不是 Git 仓库/)
    expect(gitCta).toBeInTheDocument()
    // 提交仍可用（PLAIN_DIR 是可落地路径）
    expect(screen.getByRole('button', { name: '确认添加' })).not.toHaveAttribute('aria-disabled')
  })

  it('V11 discovery complete=false → degraded banner，不显示「无匹配」结论，提交 disabled', async () => {
    stubWizardFetch({ discovery: () => ({ body: discoveryDegradedPayload }), register: registerOk })
    const user = userEvent.setup()
    const { container } = renderApp('/projects')
    await toProbeResult(user)
    await waitFor(() => expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument())
    expect(screen.queryByText(/无匹配|未匹配/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认添加' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('V12 exact_match → ALREADY_REGISTERED：主按钮「打开现有项目」，登记按钮隐藏，0 写请求', async () => {
    const stub = stubWizardFetch({ discovery: () => ({ body: discoveryExactMatchPayload }), register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('已登记')
    const openExisting = screen.getByRole('button', { name: '打开现有项目' })
    expect(screen.queryByRole('button', { name: '确认添加' })).not.toBeInTheDocument()
    const postsBefore = stub.posts.length
    await user.click(openExisting)
    // 纯导航：无新增写请求；SLICE-001 起 ProjectScope 用 Registry 权威解析，beta 在列表中 →
    // 进入其 workbench 并恢复 selection（不再因 legacy 未 stub 假报「项目不存在」）
    expect(stub.posts.length).toBe(postsBefore)
    await waitFor(() => {
      expect(screen.getByTitle('切换项目')).toHaveTextContent('Beta 项目')
    })
    expect(screen.queryByText('项目不存在')).not.toBeInTheDocument()
  })

  it('V13 possible_projects 命中 → FINGERPRINT_MATCH：attach 按钮 disabled + reason', async () => {
    stubWizardFetch({ discovery: () => ({ body: discoveryPossiblePayload }), register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    const attach = await screen.findByRole('button', { name: '关联到已登记项目' })
    expect(attach).toHaveAttribute('aria-disabled', 'true')
    const descId = attach.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
  })

  it('B2 目录 partial：本地目录仍渲染 + degraded banner，无任何「已登记/未登记」结论 tag', async () => {
    stubWizardFetch({
      gets: { '/api/runtime-nodes/local/directories': directoriesPartialPayload },
      discovery: discoveryOk,
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toDirStep(user)
    const dialog = screen.getByRole('dialog', { name: '添加项目' })
    await waitFor(() => expect(dialog.querySelector('[data-state="degraded"]')).toBeInTheDocument())
    // 目录行仍在
    expect(screen.getByRole('button', { name: /^alpha/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^beta/ })).toBeInTheDocument()
    // registered_project=null 是「未知」：不得出现结论 tag
    expect(dialog.textContent).not.toContain('已登记')
    expect(dialog.textContent).not.toContain('未登记')
  })

  it('B3a discovery write=false：向导可全程浏览，提交 disabled 且 0 register POST', async () => {
    const readOnlyDiscovery = {
      ...discoveryGitPayload,
      meta: { ...metaOk, capabilities: { 'projectRegistry.write': false } },
    }
    const stub = stubWizardFetch({
      discovery: () => ({ body: readOnlyDiscovery }),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    // 列表照常 ready（读不受 write=false 影响）
    expect(await screen.findByText('Alpha 项目')).toBeInTheDocument()
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    const submit = screen.getByRole('button', { name: '确认添加' })
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    await user.click(submit)
    expect(stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))).toEqual([])
  })

  it('B3b readonly 项目列表下，本次 discovery write=true 自举提交权限', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    const submit = screen.getByRole('button', { name: '确认添加' })
    expect(submit).not.toHaveAttribute('aria-disabled')
    await user.click(submit)
    await screen.findByText('添加成功')
    expect(stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects')).length).toBe(1)
  })

  it('B3c 返回目录后旧 discovery capability 不授权新 fingerprint', async () => {
    let discoveryCount = 0
    const readOnlySecondDiscovery = {
      ...discoveryGitPayload,
      data: {
        ...discoveryGitPayload.data,
        discovery_fingerprint: `sha256:${'9'.repeat(64)}`,
      },
      meta: { ...metaOk, capabilities: { 'projectRegistry.write': false } },
    }
    const stub = stubWizardFetch({
      discovery: () => ({ body: discoveryCount++ === 0 ? discoveryGitPayload : readOnlySecondDiscovery }),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    expect(await screen.findByRole('button', { name: '确认添加' })).not.toHaveAttribute('aria-disabled')

    await user.click(screen.getByRole('button', { name: '返回选择目录' }))
    await user.click(screen.getByRole('button', { name: /^alpha/ }))
    await user.click(screen.getByRole('button', { name: '检查并继续' }))
    expect(await screen.findByRole('button', { name: '确认添加' })).toHaveAttribute('aria-disabled', 'true')
    expect(stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))).toEqual([])
  })

  it('V17 slug 前端校验：Foo / -a / a--b / 65 字符即时拒绝，0 请求', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    const slugInput = screen.getByLabelText('标识符')
    for (const bad of ['Foo', '-a', 'a--b', 'a'.repeat(65)]) {
      await user.clear(slugInput)
      await user.type(slugInput, bad)
      // role=alert 的校验提示（提交按钮的 sr-only reason 也含同文案，故用 role 收窄）
      expect(screen.getByRole('alert').textContent).toMatch(/标识符格式无效/)
      expect(screen.getByRole('button', { name: '确认添加' })).toHaveAttribute('aria-disabled', 'true')
    }
    expect(stub.posts.filter((p) => p.url.includes('/api/project-registry'))).toEqual([])
  })

  it('V20 observed_at（+00:00 偏移）可解析且页面渲染不抛错', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    expect(Number.isNaN(Date.parse(discoveryGitPayload.data.observed_at))).toBe(false)
    expect(discoveryGitPayload.data.observed_at).toContain('+00:00')
    // 结果行只显示项目目录 + Git 信息（不渲染 locale 时间）
    const info = screen.getByText(/项目目录：/)
    expect(info.textContent).toContain(discoveryGitPayload.data.display_path)
    expect(info.textContent).toContain('Git 仓库')
  })
})

describe('WEB-003 提交（V14–V16）', () => {
  it('V14 提交 headers/body：Idempotency-Key 为 UUID，fingerprint=最近 complete 探测值，无绝对路径', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText('添加成功')
    const posts = stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))
    expect(posts.length).toBe(1)
    expect(posts[0].headers['Idempotency-Key']).toMatch(UUID_RE)
    const body = JSON.parse(posts[0].body)
    expect(body.expected_discovery_fingerprint).toBe(discoveryGitPayload.data.discovery_fingerprint)
    expect(body.locator).toEqual({ node_id: 'local', root_id: expect.stringMatching(ROOT_ID_RE), path: 'alpha' })
    expect(posts[0].body).not.toContain('/repos')
    // B6：只读 discovery POST 不带也不消费登记幂等键
    const probes = stub.posts.filter((p) => p.url.startsWith('/api/project-discovery'))
    expect(probes.length).toBe(1)
    expect(probes[0].headers['Idempotency-Key']).toBeUndefined()
  })

  it('V15 提交重试幂等：503 后重试，两次 Idempotency-Key 相同、body 逐字节相同', async () => {
    let calls = 0
    const stub = stubWizardFetch({
      discovery: discoveryOk,
      register: () => {
        calls += 1
        return calls === 1
          ? { status: 503, body: { error: { code: 'server_error', message: '暂不可用', retryable: true } } }
          : { body: registerCreatedPayload }
      },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    const retry = await screen.findByRole('button', { name: '重试' })
    await user.click(retry)
    await screen.findByText('添加成功')
    const posts = stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))
    expect(posts.length).toBe(2)
    expect(posts[0].headers['Idempotency-Key']).toBe(posts[1].headers['Idempotency-Key'])
    expect(posts[0].body).toBe(posts[1].body)
  })

  it('V16a 409 project_slug_conflict → 原地改名提示', async () => {
    stubWizardFetch({
      discovery: discoveryOk,
      register: () => ({ status: 409, body: { error: { code: 'project_slug_conflict', message: 'slug 已占用', retryable: false } } }),
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText(/换一个名称/)
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  it('V16b 409 location_already_registered → 「打开现有项目」', async () => {
    stubWizardFetch({
      discovery: discoveryOk,
      register: () => ({ status: 409, body: { error: { code: 'location_already_registered', message: '目录已登记', retryable: false } } }),
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByRole('button', { name: '打开现有项目' })
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  it('V16c 未知 409（discovery_stale 等）→ 「重新探测」回目录步', async () => {
    stubWizardFetch({
      discovery: discoveryOk,
      register: () => ({ status: 409, body: { error: { code: 'discovery_stale', message: '目录状态已变化', retryable: false } } }),
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    const reProbe = await screen.findByRole('button', { name: '重新探测' })
    await user.click(reProbe)
    // 回到目录步：目录行再次出现
    await screen.findByRole('button', { name: /^alpha/ })
  })

  it('V16d 409 idempotency_conflict → typed error 无重试', async () => {
    stubWizardFetch({
      discovery: discoveryOk,
      register: () => ({ status: 409, body: { error: { code: 'idempotency_conflict', message: '同 key 不同 body', retryable: false } } }),
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText(/取消后重开/)
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })
})

describe('WEB-003 零请求硬门与取消（V18–V19）', () => {
  it('V18a remote/离线节点卡片激活 → 0 请求', async () => {
    // 多可用节点 fixture：位置步不被自动跳过，disabled 卡片照常渲染
    const stub = stubWizardFetch({
      gets: { '/api/runtime-nodes': runtimeNodesMultiUsablePayload },
      discovery: discoveryOk,
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    const remote = (await screen.findByText('远程 GPU 节点')).closest('[aria-disabled]') as HTMLElement
    expect(remote).toHaveAttribute('aria-disabled', 'true')
    const before = stub.gets.length + stub.posts.length
    await user.click(remote)
    ;(remote as HTMLElement).focus()
    await user.keyboard('{Enter}')
    expect(stub.gets.length + stub.posts.length).toBe(before)
    expect(screen.queryByRole('button', { name: '代码' })).not.toBeInTheDocument()
  })

  it('V18b degraded 探测后提交按钮 disabled，激活 0 写请求', async () => {
    const stub = stubWizardFetch({ discovery: () => ({ body: discoveryDegradedPayload }), register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    const submit = await screen.findByRole('button', { name: '确认添加' })
    expect(submit).toHaveAttribute('aria-disabled', 'true')
    const before = stub.posts.length
    await user.click(submit)
    expect(stub.posts.length).toBe(before)
  })

  it('V18c probe 在途重复点击识别 → 只发 1 个 discovery POST', async () => {
    let resolveDiscovery: () => void = () => {}
    const stub = stubWizardFetch({
      discovery: () =>
        new Promise<MockResponseSpec>((res) => {
          resolveDiscovery = () => res({ body: discoveryGitPayload })
        }),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toDirStep(user)
    await user.click(screen.getByRole('button', { name: /^alpha/ }))
    const probeBtn = screen.getByRole('button', { name: '检查并继续' })
    await user.click(probeBtn)
    // 在途：按钮 disabled，重复点击无效
    expect(probeBtn).toHaveAttribute('aria-disabled', 'true')
    await user.click(probeBtn)
    resolveDiscovery()
    await screen.findByText('新 Git 项目')
    expect(stub.posts.filter((p) => p.url.startsWith('/api/project-discovery')).length).toBe(1)
  })

  it('WEB-005 probe 在途改选目录后丢弃旧 response 与 capability', async () => {
    let resolveDiscovery: (spec: MockResponseSpec) => void = () => {}
    const stub = stubWizardFetch({
      discovery: () => new Promise<MockResponseSpec>((resolve) => { resolveDiscovery = resolve }),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toDirStep(user)
    await user.click(screen.getByRole('button', { name: /^alpha/ }))
    await user.click(screen.getByRole('button', { name: '检查并继续' }))
    await user.click(screen.getByRole('button', { name: /^beta/ }))
    resolveDiscovery({ body: discoveryGitPayload })

    await waitFor(() => expect(stub.posts.filter((p) => p.url.startsWith('/api/project-discovery'))).toHaveLength(1))
    expect(screen.queryByText('新 Git 项目')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认添加' })).not.toBeInTheDocument()
  })

  it('V19 取消：Esc 0 写请求 + 焦点恢复触发按钮；重开后 Idempotency-Key 重新生成', async () => {
    const stub = stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    const trigger = await screen.findByRole('button', { name: '添加项目' })
    await user.click(trigger)
    await screen.findByRole('dialog', { name: '添加项目' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '添加项目' })).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
    expect(stub.posts).toEqual([])

    // 会话 1：走到提交，捕获 key1
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText('添加成功')
    await user.keyboard('{Escape}')

    // 会话 2：重开后再次提交，key 必须重新生成
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findAllByText('添加成功')
    const keys = stub.posts
      .filter((p) => p.url.startsWith('/api/project-registry/projects'))
      .map((p) => p.headers['Idempotency-Key'])
    expect(keys.length).toBe(2)
    expect(keys[0]).toMatch(UUID_RE)
    expect(keys[1]).toMatch(UUID_RE)
    expect(keys[0]).not.toBe(keys[1])
  })
})

// ================= 返修 owned 用例（独立 QA B1/B2 固定） =================

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

describe('返修1–3：逐字段守卫 fail-closed', () => {
  const validDiscovery = discoveryGitPayload.data
  const validDirectory = directoriesPayload.data

  it('nullable string 错型（head=1 / git_root_digest=false）→ ProtocolError', () => {
    const a = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    a.vcs.head = 1
    expect(() => assertDiscoveryResultData(a)).toThrow(ProtocolError)
    const b = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    b.vcs.git_root_digest = false
    expect(() => assertDiscoveryResultData(b)).toThrow(ProtocolError)
  })

  it('nullable int 错型（ahead="0" / behind=false）→ ProtocolError', () => {
    const a = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    a.vcs.ahead = '0'
    expect(() => assertDiscoveryResultData(a)).toThrow(ProtocolError)
    const b = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    b.vcs.behind = false
    expect(() => assertDiscoveryResultData(b)).toThrow(ProtocolError)
  })

  it('bool 缺失（删 detached/unborn/dirty）→ ProtocolError', () => {
    for (const field of ['detached', 'unborn', 'dirty', 'branch_present', 'upstream_present']) {
      const v = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
      delete v.vcs[field]
      expect(() => assertDiscoveryResultData(v), field).toThrow(ProtocolError)
    }
  })

  it('enum 非法（kind="svn"）→ ProtocolError；refs_count 非整数 → ProtocolError', () => {
    const a = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    a.vcs.kind = 'svn'
    expect(() => assertDiscoveryResultData(a)).toThrow(ProtocolError)
    const b = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    b.vcs.refs_count = '3'
    expect(() => assertDiscoveryResultData(b)).toThrow(ProtocolError)
    const c = clone(validDiscovery) as unknown as { vcs: Record<string, unknown> }
    c.vcs.refs_count = 3.5
    expect(() => assertDiscoveryResultData(c)).toThrow(ProtocolError)
  })

  it('sources/warnings 缺失 → ProtocolError（不得合成 []）；非 string 元素 → ProtocolError', () => {
    for (const field of ['sources', 'warnings']) {
      const v = clone(validDiscovery) as unknown as Record<string, unknown>
      delete v[field]
      expect(() => assertDiscoveryResultData(v), `discovery.${field}`).toThrow(ProtocolError)
      const d = clone(validDirectory) as unknown as Record<string, unknown>
      delete d[field]
      expect(() => assertDirectoryListingData(d), `directories.${field}`).toThrow(ProtocolError)
    }
    const badDiscovery = clone(validDiscovery) as unknown as Record<string, unknown>
    badDiscovery.sources = [1]
    expect(() => assertDiscoveryResultData(badDiscovery)).toThrow(ProtocolError)
    const badDir = clone(validDirectory) as unknown as Record<string, unknown>
    badDir.warnings = [false]
    expect(() => assertDirectoryListingData(badDir)).toThrow(ProtocolError)
    // directory partial 必填
    const noPartial = clone(validDirectory) as unknown as Record<string, unknown>
    delete noPartial.partial
    expect(() => assertDirectoryListingData(noPartial)).toThrow(ProtocolError)
  })

  it('RegistryMatch 三字段各缺一/错型 → ProtocolError（registered_project / exact_match / possible_projects）', () => {
    const match = { project_id: `prj_${'a1'.repeat(16)}`, slug: 'alpha', display_name: 'Alpha 项目' }
    for (const field of ['project_id', 'slug', 'display_name']) {
      const dir = clone(validDirectory) as unknown as { entries: Array<Record<string, unknown>> }
      const m = { ...match } as Record<string, unknown>
      delete m[field]
      dir.entries[0].registered_project = m
      expect(() => assertDirectoryListingData(dir), `registered_project.${field}`).toThrow(ProtocolError)

      const dis = clone(validDiscovery) as unknown as Record<string, unknown>
      const em = { ...match } as Record<string, unknown>
      em[field] = 1
      dis.exact_match = em
      expect(() => assertDiscoveryResultData(dis), `exact_match.${field}`).toThrow(ProtocolError)

      const dis2 = clone(validDiscovery) as unknown as Record<string, unknown>
      const pm = { ...match } as Record<string, unknown>
      delete pm[field]
      dis2.possible_projects = [pm]
      expect(() => assertDiscoveryResultData(dis2), `possible_projects.${field}`).toThrow(ProtocolError)
    }
    // possible_projects 非数组 → ProtocolError；registered_project 怪类型 → ProtocolError
    const badPossible = clone(validDiscovery) as unknown as Record<string, unknown>
    badPossible.possible_projects = 'nope'
    expect(() => assertDiscoveryResultData(badPossible)).toThrow(ProtocolError)
    const badRegistered = clone(validDirectory) as unknown as { entries: Array<Record<string, unknown>> }
    badRegistered.entries[0].registered_project = 42
    expect(() => assertDirectoryListingData(badRegistered)).toThrow(ProtocolError)
    // null/undefined 合法
    expect(assertDiscoveryResultData(clone(validDiscovery)).exact_match).toBeNull()
  })
})

describe('返修4：幂等键绑定序列化 body', () => {
  it('submit→503→改 slug→Retry：body 变化 → 新 Idempotency-Key', async () => {
    let n = 0
    const stub = stubWizardFetch({
      discovery: discoveryOk,
      register: () => {
        n += 1
        return n === 1
          ? { status: 503, body: { error: { code: 'server_error', message: '暂不可用', retryable: true } } }
          : { body: registerCreatedPayload }
      },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByRole('button', { name: '重试' })
    const slugInput = screen.getByLabelText('标识符')
    await user.clear(slugInput)
    await user.type(slugInput, 'alpha-2')
    await user.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByText('添加成功')
    const posts = stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))
    expect(posts.length).toBe(2)
    expect(posts[0].body).not.toBe(posts[1].body)
    expect(posts[0].headers['Idempotency-Key']).not.toBe(posts[1].headers['Idempotency-Key'])
  })

  it('submit→409 stale→回目录改选 locator→新 probe→提交：新 Idempotency-Key', async () => {
    let n = 0
    const stub = stubWizardFetch({
      discovery: discoveryOk,
      register: () => {
        n += 1
        return n === 1
          ? { status: 409, body: { error: { code: 'discovery_stale', message: '目录状态已变化', retryable: false } } }
          : { body: registerCreatedPayload }
      },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user, 'alpha')
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await user.click(await screen.findByRole('button', { name: '重新探测' }))
    await user.click(await screen.findByRole('button', { name: /^beta/ }))
    await user.click(screen.getByRole('button', { name: '检查并继续' }))
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText('添加成功')
    const posts = stub.posts.filter((p) => p.url.startsWith('/api/project-registry/projects'))
    expect(posts.length).toBe(2)
    expect(posts[0].body).not.toBe(posts[1].body)
    expect(posts[0].headers['Idempotency-Key']).not.toBe(posts[1].headers['Idempotency-Key'])
    // V15 已锁定反向：body 逐字节相同的 retry 复用同 key
  })
})

// ---------- 黄金路径（首用前半链）：入口汇聚 / 自动跳过 / 错误恢复 / 成功导航 ----------

describe('黄金路径：位置与代码位置自动跳过', () => {
  it('G1 唯一可用 local 节点（其余 disabled）→ 打开即代码位置列表，无位置步', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    // 不点任何节点卡，直接出现 root 列表
    expect(await screen.findByRole('button', { name: '代码' })).toBeInTheDocument()
    expect(screen.queryByText('远程 GPU 节点')).toBeNull()
  })

  it('G2 两个可用 local 节点 → 位置步照常展示，不自动跳过', async () => {
    stubWizardFetch({
      gets: { '/api/runtime-nodes': runtimeNodesMultiUsablePayload },
      discovery: discoveryOk,
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    expect(await screen.findByRole('button', { name: /第二台本机/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^本机/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '代码' })).toBeNull()
  })

  it('G3 唯一代码位置 → 自动展开其目录列表；多个则仍需选择', async () => {
    stubWizardFetch({
      gets: {
        '/api/runtime-nodes/local/roots': {
          data: { items: [rootsPayload.data.items[0]] },
          meta: metaOk,
        },
      },
      discovery: discoveryOk,
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    // 不点 root，直接出现目录行与「更换代码位置」
    expect(await screen.findByRole('button', { name: /^alpha/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '更换代码位置' })).toBeInTheDocument()
  })
})

describe('黄金路径：识别失败可恢复（用户语言）', () => {
  const probeError = (status: number, code: string, message: string) => () => ({
    status,
    body: { error: { code, message, retryable: false } },
  })

  it('G4 root_forbidden → 明确文案 + 返回选择目录可恢复', async () => {
    stubWizardFetch({
      discovery: probeError(403, 'root_forbidden', 'root forbidden'),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    expect(await screen.findByText('不能登记这个代码位置本身')).toBeInTheDocument()
    expect(screen.getByText(/同一代码位置内直接包含 \.git 的仓库目录/)).toBeInTheDocument()
    // 恢复动作：返回选择目录后目录行重新可选
    await user.click(screen.getByRole('button', { name: '返回选择目录' }))
    expect(await screen.findByRole('button', { name: /^alpha/ })).toBeInTheDocument()
  })

  it('G5 invalid_locator → 提示选择直接包含 .git 的仓库根目录', async () => {
    stubWizardFetch({
      discovery: probeError(400, 'invalid_locator', 'invalid locator'),
      register: registerOk,
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    expect(await screen.findByText('这个目录不能登记')).toBeInTheDocument()
    expect(screen.getByText(/直接包含 \.git 的仓库根目录/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '返回选择目录' })).toBeInTheDocument()
  })
})

describe('黄金路径：登记成功导航', () => {
  it('G6 成功卡无 Project ID/Slug  jargon，主按钮进入 Workbench（?createWorkspace=1）', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    const user = userEvent.setup()
    renderApp('/projects')
    await toProbeResult(user)
    await screen.findByText('新 Git 项目')
    await user.click(screen.getByRole('button', { name: '确认添加' }))
    await screen.findByText('添加成功')
    const dialog = screen.getByRole('dialog', { name: '添加项目' })
    expect(dialog.textContent).not.toContain('Project ID')
    expect(dialog.textContent).not.toContain('Workbench 将在')
    await user.click(screen.getByRole('button', { name: '继续创建工作空间' }))
    // 导航到该项目 Workbench（createWorkspace 意图由 URL 携带，见 routes.test.ts 合同断言）
    expect(await screen.findByText('项目概览', { selector: '.page-sub' })).toBeInTheDocument()
  })
})

describe('黄金路径：四入口汇聚同一个向导', () => {
  it('G7 Overview 零项目 → 选择代码目录 CTA（registry 门控，与 attention 无关）', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    renderApp('/overview')
    const cta = await screen.findByRole('link', { name: '选择代码目录' })
    expect(cta.getAttribute('href')).toContain('/projects?wizard=1')
  })

  it('G8 Overview 有项目 → 不出现首用 CTA', async () => {
    stubFetch(defaultFetchMap())
    renderApp('/overview')
    await screen.findByText('需要你处理')
    await waitFor(() => expect(screen.queryByText('正在汇总工作…')).toBeNull())
    expect(screen.queryByRole('link', { name: '选择代码目录' })).toBeNull()
  })

  it('G8b Overview 无法读取项目列表时显示明确重试，并可恢复首用入口', async () => {
    let recovered = false
    const map = defaultFetchMap()
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects')) {
        if (!recovered) {
          return {
            status: 403,
            body: { error: { code: 'forbidden', message: '项目列表不可用', retryable: false } },
          }
        }
        return { body: registryProjectsEmptyPayload }
      }
      return url in map ? { body: map[url] } : undefined
    })
    const user = userEvent.setup()
    renderApp('/overview')
    expect(await screen.findByText('项目列表暂不可用')).toBeInTheDocument()
    recovered = true
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByRole('link', { name: '选择代码目录' })).toBeInTheDocument()
  })

  it('G9 Welcome 主按钮 → 同一个向导入口', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/overview': {
        projects: [],
        total_unread: 0,
        total_projects: 0,
        total_agents: 0,
        agent_mail: { available: false, reason: 'none' },
      },
    })
    renderApp('/welcome')
    const cta = await screen.findByRole('link', { name: '选择代码目录' })
    expect(cta.getAttribute('href')).toContain('/projects?wizard=1')
  })

  it('G10 ?wizard=1 深链直接打开向导', async () => {
    stubWizardFetch({ discovery: discoveryOk, register: registerOk })
    renderApp('/projects?wizard=1')
    expect(await screen.findByRole('dialog', { name: '添加项目' })).toBeInTheDocument()
  })

  it('G11 ProjectDrawer 零项目 → 选择代码目录：先关抽屉再开向导', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    const user = userEvent.setup()
    renderApp('/overview')
    await user.click(await screen.findByTitle('切换项目'))
    const drawer = await screen.findByRole('dialog', { name: '项目切换' })
    await user.click(within(drawer).getByRole('button', { name: '选择代码目录' }))
    // drawer 关闭（不在 wizard overlay 之后残留），向导可见
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: '项目切换' })).toBeNull(),
    )
    expect(await screen.findByRole('dialog', { name: '添加项目' })).toBeInTheDocument()
  })

  it('G12 ProjectDrawer 列表与 Projects 页同一 Registry 权威（display_name 渲染）', async () => {
    stubFetch(defaultFetchMap())
    const user = userEvent.setup()
    renderApp('/overview')
    await user.click(await screen.findByTitle('切换项目'))
    const drawer = await screen.findByRole('dialog', { name: '项目切换' })
    expect(await within(drawer).findByText('Alpha 项目')).toBeInTheDocument()
    expect(within(drawer).getByText('Project Two')).toBeInTheDocument()
  })
})

// ---------- 首用恢复：成功空数组的明确空态（不白屏） ----------

describe('向导空态（成功空数组）', () => {
  it('nodes 空数组：明确空态 + 重新检查 + 取消，不白屏', async () => {
    stubWizardFetch({
      gets: { '/api/runtime-nodes': { data: { nodes: [] }, meta: metaOk } },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    await screen.findByText('没有发现可用的位置')
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument()
  })

  it('roots 空数组：空态 + 重新检查 + 返回选择位置（不被自动跳过弹回）', async () => {
    stubWizardFetch({
      gets: { '/api/runtime-nodes/local/roots': { data: { items: [] }, meta: metaOk } },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    const dialog = await openWizard(user)
    await screen.findByText('这个位置下没有代码位置')
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '返回选择位置' }))
    // 回到位置步且停留（手动返回后不再自动跳过）：本机节点卡片可见
    await within(dialog).findByRole('button', { name: /本机/ })
  })

  it('directories 空数组：空态 + 重新检查 + 更换代码位置', async () => {
    stubWizardFetch({
      gets: {
        '/api/runtime-nodes/local/directories': {
          data: {
            locator: { node_id: 'local', root_id: 'root_0123456789abcdef01234567', path: '' },
            entries: [],
            complete: true,
            partial: false,
            sources: ['local_files'],
            warnings: [],
          },
          meta: metaOk,
        },
      },
    })
    const user = userEvent.setup()
    renderApp('/projects')
    await openWizard(user)
    await user.click(await screen.findByRole('button', { name: '代码' }))
    await screen.findByText('这里还没有可选择的项目目录')
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument()
    // 空态自带恢复动作（与列表头部既有 ghost 按钮同名，至少一处可用）
    expect(screen.getAllByRole('button', { name: '更换代码位置' }).length).toBeGreaterThanOrEqual(1)
  })
})
