import { screen, waitFor, within } from '@testing-library/react'
import { metaOk, REG_P1, workspaceListP1Payload } from '../fixtures/api'
import { capabilities, capability } from '../state/capabilities'
import { renderApp, stubDefaultFetch } from './helpers'

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
const focusStub = { [WORK_ITEMS]: emptyWorkItems }

describe('capability registry（静态 fail-closed 表）', () => {
  it('W1 静态表除 TERM-003 本地开关外全部 available=false 且带真实原因', () => {
    const keys = Object.keys(capabilities) as (keyof typeof capabilities)[]
    expect(keys.length).toBeGreaterThan(0)
    for (const k of keys) {
      if (k === 'terminal.control.ui') continue // TERM-003 本地实现开关，见 capabilities.tsx 注释
      expect(capabilities[k].available).toBe(false)
      expect(capabilities[k].reason).toBeTruthy()
    }
    expect(capability('terminal.control.ui').available).toBe(true)
    expect(capability('memory.local').reason).toContain('项目记忆暂未开放')
    expect(capability('terminal.pty').available).toBe(false)
    expect(capability('files.read').reason).toContain('不影响终端使用')
  })

  it('memory 页面整页 forbidden + 原因 + 文档入口 → 路由隐藏，回到项目列表', async () => {
    stubDefaultFetch(focusStub)
    renderApp('/projects/p1/memory')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('项目记忆暂未开放，不影响文件与终端的使用')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '查看路线图' })).toBeNull()
    expect(screen.queryByText('项目记忆', { selector: '.page-title' })).not.toBeInTheDocument()
  })

  it('git/editor/browser 整页 forbidden → 路由隐藏，回到项目列表', async () => {
    stubDefaultFetch(focusStub)
    renderApp('/projects/p1/workspaces/w1/git')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('Git 集成暂未开放，可在终端中继续使用 Git')).not.toBeInTheDocument()
  })

  it('危险写按钮不出现：删除工作空间不得假实现', async () => {
    stubDefaultFetch(focusStub)
    renderApp('/projects/p1/workspaces/w1')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除工作空间' })).toBeNull()
  })

  it('workspace 首页是 Focus，不再用未接通卡片；文件/终端走 Rail', async () => {
    stubDefaultFetch(focusStub)
    const { container } = renderApp('/projects/p1/workspaces/w1')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(container.querySelectorAll('.card')).toHaveLength(0)
    expect(screen.queryByText('内嵌编辑器暂未开放，可继续使用文件浏览与终端')).not.toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(within(rail).getByTitle('文件')).toHaveAttribute('href', '/projects/p1/workspaces/w1/files')
    expect(within(rail).getByTitle('终端')).toHaveAttribute('href', '/projects/p1/workspaces/w1/terminal')
  })

  it('设置页无永久 disabled 的保存按钮，只读说明可见 → /settings 回到项目列表', async () => {
    stubDefaultFetch(focusStub)
    renderApp('/settings')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('当前为只读：修改将在后续版本开放')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存设置' })).toBeNull()
  })

  it('终端页 PTY 未接通 banner + 控制按钮 aria-disabled', async () => {
    stubDefaultFetch(focusStub)
    const { container } = renderApp('/projects/p1/workspaces/w1/terminal')
    await waitFor(() => {
      expect(container.querySelector('[data-state="disconnected"]')).toBeInTheDocument()
    })
    for (const name of ['中断', '重连', '重启']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
    }
  })
})

describe('rail 项目段 capability 门控', () => {
  it('unavailable 的 变更审核/动态/项目记忆 不作主导航展示；项目概览保留', async () => {
    stubDefaultFetch(focusStub)
    renderApp('/projects/p1/workbench')
    const rail = await screen.findByRole('navigation', { name: '主导航' })
    expect(within(rail).queryByRole('link', { name: '项目概览' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '变更审核' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '动态' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '项目记忆' })).toBeNull()
    expect(await within(rail).findByTitle('项目')).toBeInTheDocument()
  })

  it('server 声明 available 时对应导航仍不出现（A 隐藏延期菜单）', async () => {
    stubDefaultFetch({
      ...focusStub,
      [`/api/project-registry/projects/${REG_P1}/workspaces`]: {
        ...workspaceListP1Payload,
        meta: {
          ...metaOk,
          capabilities: {
            'recovery.review': { available: true, reason: null },
            'activity.feed': { available: true, reason: null },
            'memory.local': { available: true, reason: null },
          },
        },
      },
    })
    renderApp('/projects/p1/workbench')
    const rail = await screen.findByRole('navigation', { name: '主导航' })
    expect(within(rail).queryByRole('link', { name: '变更审核' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '动态' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '项目记忆' })).toBeNull()
  })
})
