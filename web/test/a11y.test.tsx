import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from '../components/Button'
import { renderApp, stubDefaultFetch } from './helpers'

const legacyOverrides = {
  '/api/overview': { projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: { available: true } },
  '/api/attention': { sessions: [], items: [], count: 0, mail_unread: 0, capabilities: {} },
  '/api/herdr/status': { available: true, binary: '/usr/local/bin/herdr' },
  '/api/settings': { language: 'zh', known_agents: ['claude'], languages: ['zh', 'en'] },
}

describe('A11y（item 7）', () => {
  it('skip link（button 形态）聚焦主内容，不改写业务 hash', async () => {
    stubDefaultFetch(legacyOverrides)
    const user = userEvent.setup()
    const { container } = renderApp('/overview')
    const skip = screen.getByRole('button', { name: '跳到主内容' })
    // 不是 anchor——HashRouter 下 href="#main-content" 会改写业务 hash
    expect(skip.tagName).toBe('BUTTON')
    await user.click(skip)
    const main = container.querySelector('#main-content')
    expect(main).not.toBeNull()
    expect(main).toHaveAttribute('tabindex', '-1')
    expect(document.activeElement).toBe(main)
    // 页面仍停留在 overview（URL/路由未被改写）
    expect(await screen.findByText('需要你处理', { selector: '.page-title' })).toBeInTheDocument()
  })

  it('settings tabs：roving tabindex + ArrowRight/Home/End（激活跟随焦点）', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    const { container } = renderApp('/settings')

    const harness = await screen.findByRole('tab', { name: 'Harness / Runtime 与节点' })
    expect(harness).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: '外观' })).toHaveAttribute('tabindex', '-1')
    expect(harness).toHaveAttribute('aria-controls', 'panel-harness')

    await user.click(harness) // 聚焦 active tab
    await user.keyboard('{ArrowRight}')
    const appearance = screen.getByRole('tab', { name: '外观' })
    expect(appearance).toHaveAttribute('aria-selected', 'true')
    expect(appearance).toHaveAttribute('tabindex', '0')
    expect(appearance).toHaveFocus()
    expect(harness).toHaveAttribute('tabindex', '-1')

    await user.keyboard('{End}')
    const doctor = screen.getByRole('tab', { name: '环境自检' })
    expect(doctor).toHaveAttribute('aria-selected', 'true')
    expect(doctor).toHaveFocus()
    expect(container.querySelector('#panel-doctor')).not.toBeNull()

    await user.keyboard('{Home}')
    expect(harness).toHaveAttribute('aria-selected', 'true')
    expect(harness).toHaveFocus()

    // 循环：首 tab 上按 ArrowLeft → 跳到末尾
    await user.keyboard('{ArrowLeft}')
    expect(doctor).toHaveAttribute('aria-selected', 'true')
    expect(doctor).toHaveFocus()
  })

  it('aria-disabled 按钮可聚焦、aria-describedby 关联 reason 节点、激活无效', async () => {
    const onClick = vi.fn()
    const { container } = render(
      <Button disabled title="Workspace 删除未开放（W1 只读骨架）" onClick={onClick}>
        删除
      </Button>,
    )
    const btn = screen.getByRole('button', { name: '删除' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', 'Workspace 删除未开放（W1 只读骨架）')
    // reason 不只靠 title：aria-describedby 关联的节点存在且含 reason 文本
    const descId = btn.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
    const desc = container.querySelector(`#${CSS.escape(descId!)}`)
    expect(desc).not.toBeNull()
    expect(desc?.textContent).toContain('Workspace 删除未开放')
    ;(btn as HTMLElement).focus()
    expect(btn).toHaveFocus()

    const user = userEvent.setup()
    await user.click(btn)
    await user.keyboard('{Enter}')
    await user.keyboard(' ')
    expect(onClick).not.toHaveBeenCalled()
  })

  it('390 核心 Rail 与 Workspace 主导航只保留工作空间概览、文件、终端', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workspaces/w1/files')
    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => expect(within(rail).getByTitle('文件')).toBeInTheDocument())

    for (const title of ['项目', '文件', '终端']) {
      const item = within(rail).getByTitle(title)
      expect(item).toHaveClass('rail-item--mobile-core')
      expect(item.querySelector('.rail-mobile-label')).toHaveTextContent(title)
    }
    expect(within(rail).getByTitle('需要你处理')).not.toHaveClass('rail-item--mobile-core')
    const workspaceSection = within(rail).getByText('当前工作空间').closest('.rail-section')
    expect(workspaceSection).not.toBeNull()
    expect(
      Array.from(workspaceSection!.querySelectorAll<HTMLElement>('.rail-item')).map(
        (item) => item.title,
      ),
    ).toEqual(['工作空间概览', '文件', '终端'])
  })
})
