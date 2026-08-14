import { screen, waitFor, within } from '@testing-library/react'
import { metaOk, REG_P1, workspaceListP1Payload } from '../fixtures/api'
import { capabilities, capability } from '../state/capabilities'
import { renderApp, stubDefaultFetch } from './helpers'

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
    expect(capability('memory.local').reason).toContain('W4')
    expect(capability('terminal.pty').available).toBe(false)
    expect(capability('files.read').reason).toContain('禁止回退全局 legacy')
  })

  it('memory 页面整页 forbidden + 原因 + 文档入口', async () => {
    stubDefaultFetch()
    const { container } = renderApp('/projects/p1/memory')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(screen.getByText('Memory/Context Pack 规划在 W4 接通')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看路线图' })).toHaveAttribute(
      'href',
      '#/settings?view=doctor',
    )
    // 页面头结构保留
    expect(screen.getByText('项目记忆', { selector: '.page-title' })).toBeInTheDocument()
  })

  it('git/editor/browser 整页 forbidden', async () => {
    stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1/git')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(screen.getByText('Git 集成 API 未接通（W1）')).toBeInTheDocument()
  })

  it('危险写按钮 aria-disabled + title 原因，可聚焦', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workspaces/w1')
    const del = await screen.findByRole('button', { name: '删除工作空间' })
    expect(del).toHaveAttribute('aria-disabled', 'true')
    expect(del).toHaveAttribute('title', 'Workspace 删除未开放（W1 只读骨架）')
    ;(del as HTMLElement).focus()
    expect(del).toHaveFocus()
  })

  it('workspace 首页未接通卡片 disabled + 原因，已接通卡片可导航', async () => {
    stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1')
    await screen.findByRole('button', { name: '删除工作空间' })
    const cards = Array.from(container.querySelectorAll('.card'))
    const editorCard = cards.find((c) => c.textContent?.includes('编辑器'))
    expect(editorCard).toHaveClass('card--disabled')
    expect(editorCard).toHaveAttribute('aria-disabled', 'true')
    expect(editorCard?.textContent).toContain('内嵌编辑器规划在后续迭代接通')
    // SLICE-001：文件卡由 files.read capability 控制；默认世界 server 未声明 → fail-closed disabled
    const filesCard = cards.find((c) => c.textContent?.includes('文件'))
    expect(filesCard).toHaveClass('card--disabled')
    expect(filesCard).toHaveAttribute('aria-disabled', 'true')
    expect(filesCard?.textContent).toContain('Workspace 文件 facade API 未接通')
  })

  it('设置写按钮 aria-disabled（W1 只读）', async () => {
    stubDefaultFetch()
    renderApp('/settings')
    const save = await screen.findByRole('button', { name: '保存设置' })
    expect(save).toHaveAttribute('aria-disabled', 'true')
    expect(save).toHaveAttribute('title', '设置写操作未开放（W1 只读）')
  })

  it('终端页 PTY 未接通 banner + 控制按钮 aria-disabled', async () => {
    stubDefaultFetch()
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
    stubDefaultFetch()
    renderApp('/projects/p1/workbench')
    const rail = await screen.findByRole('navigation', { name: '主导航' })
    await within(rail).findByRole('link', { name: '项目概览' })
    expect(within(rail).queryByRole('link', { name: '变更审核' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '动态' })).toBeNull()
    expect(within(rail).queryByRole('link', { name: '项目记忆' })).toBeNull()
  })

  it('server 声明 available 时对应导航出现', async () => {
    stubDefaultFetch({
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
    await within(rail).findByRole('link', { name: '变更审核' })
    expect(within(rail).getByRole('link', { name: '动态' })).toBeInTheDocument()
    expect(within(rail).getByRole('link', { name: '项目记忆' })).toBeInTheDocument()
  })
})
