import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubDefaultFetch } from './helpers'

describe('Remote workspace fail-closed（P1-5）+ switcher roving keyboard（P2-10）', () => {
  it('WorkspaceSwitcher：remote 项可见但 aria-disabled + 原因可读，点击零请求不跳转', async () => {
    const fetchSpy = stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1')
    await screen.findByRole('button', { name: '删除 Workspace' })

    await user.click(screen.getByTitle('切换 Workspace'))
    const dialog = await screen.findByRole('dialog', { name: 'Workspace 切换' })
    const remoteBtn = within(dialog).getByRole('button', { name: /远程 GPU/ })
    expect(remoteBtn).toHaveAttribute('aria-disabled', 'true')
    // 原因不只靠 title：aria-describedby 节点存在且含 reason
    const descId = remoteBtn.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
    const desc = dialog.querySelector(`#${CSS.escape(descId!)}`)
    expect(desc?.textContent).toContain('远程 Herdr 控制未接通')

    const callsBefore = fetchSpy.mock.calls.length
    await user.click(remoteBtn)
    await user.keyboard('{Enter}')
    // 零请求、不跳转、dialog 仍开着
    expect(fetchSpy.mock.calls.length).toBe(callsBefore)
    expect(screen.getByRole('dialog', { name: 'Workspace 切换' })).toBeInTheDocument()
    expect(screen.getByText('本机工作区', { selector: '.page-title' })).toBeInTheDocument()
  })

  it('Rail：remote workspace 渲染为 disabled 项（可聚焦、原因可读）', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workspaces/w1')
    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => {
      expect(within(rail).getByText('远程 GPU')).toBeInTheDocument()
    })
    const remoteItem = within(rail).getByText('远程 GPU').closest('[aria-disabled]')
    expect(remoteItem).toHaveAttribute('aria-disabled', 'true')
    expect(remoteItem).toHaveAttribute('tabindex', '0')
    const descId = remoteItem?.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
  })

  it('switcher roving keyboard：ArrowDown/Up/Home/End 移动焦点，Enter 选中跳转，Esc 恢复焦点', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1')
    const trigger = await screen.findByTitle('切换 Workspace')
    await user.click(trigger)
    const dialog = await screen.findByRole('dialog', { name: 'Workspace 切换' })
    const items = within(dialog).getAllByRole('button').filter((b) => b.className.includes('drawer-item'))
    expect(items.length).toBe(2)

    // roving：聚焦第一项，ArrowDown → 第二项（remote，disabled 但可聚焦）
    ;(items[0] as HTMLElement).focus()
    await user.keyboard('{ArrowDown}')
    expect(items[1]).toHaveFocus()
    // Enter 在 disabled 项上被拦截，dialog 不关
    await user.keyboard('{Enter}')
    expect(screen.getByRole('dialog', { name: 'Workspace 切换' })).toBeInTheDocument()
    // End/Home 跳首尾
    await user.keyboard('{Home}')
    expect(items[0]).toHaveFocus()
    await user.keyboard('{End}')
    expect(items[1]).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(items[0]).toHaveFocus()

    // Esc 关闭并恢复焦点到触发按钮
    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Workspace 切换' })).not.toBeInTheDocument()
    })
    expect(trigger).toHaveFocus()
  })
})
