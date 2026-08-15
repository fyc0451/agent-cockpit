import { screen, waitFor } from '@testing-library/react'
import { defaultFetchMap, metaOk, projectP1, REG_P1, workspaceW1 } from '../fixtures/api'
import { parseServerCapabilities } from '../state/capabilities'
import { renderApp, stubFetch } from './helpers'

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }

function stubWithProjectCaps(caps: unknown) {
  return stubFetch({
    ...defaultFetchMap(),
    [WORK_ITEMS]: emptyWorkItems,
    '/api/projects/p1': { data: projectP1, meta: { ...metaOk, capabilities: caps } },
  })
}

describe('meta.capabilities 权威合并层（item 6）', () => {
  it('parseServerCapabilities 宽容解析 boolean 与 object 两种形态', () => {
    expect(parseServerCapabilities(null)).toEqual({})
    expect(parseServerCapabilities('x')).toEqual({})
    const parsed = parseServerCapabilities({
      'files.read': false,
      'terminal.pty': true,
      browser: { available: true, reason: null },
      git: { available: false, reason: '后端标记关闭' },
      junk: 42,
    })
    expect(parsed['files.read'].available).toBe(false)
    expect(parsed['terminal.pty'].available).toBe(true)
    expect(parsed['terminal.pty'].reason).toBeNull()
    expect(parsed['browser'].available).toBe(true)
    expect(parsed['git'].available).toBe(false)
    expect(parsed['git'].reason).toBe('后端标记关闭')
    expect(parsed['junk']).toBeUndefined()
  })

  it('server cap available=false 缺 reason → 合成稳定可读 reason（P2-9）', () => {
    const parsed = parseServerCapabilities({
      a: { available: false },
      b: { available: false, reason: '' },
      c: false,
      d: { available: true },
    })
    expect(parsed['a'].reason).toBe('服务端未说明原因，该能力暂不可用')
    expect(parsed['b'].reason).toBe('服务端未说明原因，该能力暂不可用')
    expect(parsed['c'].reason).toBe('服务端未说明原因，该能力暂不可用')
    expect(parsed['d'].reason).toBeNull()
  })

  it('Project meta 标记 files.read=true 也不得开启 Workspace 文件能力', async () => {
    stubWithProjectCaps({ 'files.read': true })
    const { container } = renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(screen.getByText(/文件浏览暂未接通/)).toBeInTheDocument()
  })

  it('Project meta 声明 terminal.pty=true 也不得开启 Workspace PTY', async () => {
    stubWithProjectCaps({ 'terminal.pty': { available: true, reason: null } })
    const { container } = renderApp('/projects/p1/workspaces/w1/terminal')
    await waitFor(() => {
      expect(container.querySelector('[data-state="disconnected"]')).toBeInTheDocument()
    })
    expect(screen.queryByText(/已由服务端 capability 标记为可用/)).not.toBeInTheDocument()
    for (const name of ['中断', '重连', '重启']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
    }
  })

  it('Project meta 不得开启 Workspace unavailable 页与首页操作', async () => {
    stubWithProjectCaps({
      'git.integration': { available: true, reason: 'Project Git 可用' },
      'editor.embedded': { available: true, reason: 'Project Editor 可用' },
      browser: { available: true, reason: 'Project Browser 可用' },
      'workspace.delete': { available: true, reason: 'Project Delete 可用' },
    })

    const git = renderApp('/projects/p1/workspaces/w1/git')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('Git 集成暂未开放，可在终端中继续使用 Git')).not.toBeInTheDocument()
    expect(screen.queryByText('Project Git 可用')).not.toBeInTheDocument()
    git.unmount()

    const home = renderApp('/projects/p1/workspaces/w1')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除工作空间' })).toBeNull()
    expect(screen.queryByText('内嵌编辑器暂未开放，可继续使用文件浏览与终端')).not.toBeInTheDocument()
    expect(screen.queryByText('内嵌浏览器暂未开放，不影响其他功能的使用')).not.toBeInTheDocument()
    expect(screen.queryByText(/Project (Editor|Browser|Delete) 可用/)).not.toBeInTheDocument()
    home.unmount()
  })

  it('server 未提及的 key 保持 fail-closed', async () => {
    const fetchSpy = stubFetch({
      ...defaultFetchMap(),
      [WORK_ITEMS]: emptyWorkItems,
      [`/api/project-registry/projects/${REG_P1}/workspaces/w1`]: {
        data: workspaceW1,
        meta: {
          ...metaOk,
          capabilities: {
            'terminal.pty': { available: true, reason: null },
          },
        },
      },
    })
    const home = renderApp('/projects/p1/workspaces/w1')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除工作空间' })).toBeNull()
    expect(home.container.querySelectorAll('.card')).toHaveLength(0)
    expect(screen.queryByLabelText('搜索文件')).not.toBeInTheDocument()
    home.unmount()

    renderApp('/projects/p1/workspaces/w1/files')
    expect(await screen.findByText('文件浏览暂不可用')).toBeInTheDocument()
    expect(screen.getByText(/文件浏览暂未接通/)).toBeInTheDocument()
    expect(screen.queryByLabelText('搜索文件')).not.toBeInTheDocument()
    const fileCalls = fetchSpy.mock.calls
      .map((c) => String(c[0]))
      .filter((url) => url.includes('/files'))
    expect(fileCalls).toEqual([])
  })
})
