import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { metaOk, REG_P1, workspaceW1, workspaceW2 } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
const localTwo = { ...workspaceW1, workspace_id: 'w-local-2', name: '第二本机' }
const twoLocalAndRemote = {
  [`/api/projects/${REG_P1}/workspaces/w1/work-items`]: emptyWorkItems,
  [`/api/projects/${REG_P1}/workspaces/w-local-2/work-items`]: emptyWorkItems,
  [`/api/project-registry/projects/${REG_P1}/workspaces`]: {
    data: { items: [workspaceW1, localTwo, workspaceW2] },
    meta: metaOk,
  },
  [`/api/project-registry/projects/${REG_P1}/workspaces/w-local-2`]: {
    data: localTwo,
    meta: metaOk,
  },
}

describe('Remote workspace fail-closed（P1-5）+ switcher roving keyboard（P2-10）', () => {
  it('WorkspaceSwitcher：remote 项隐藏，两个本机项可见，点 remote 名零请求', async () => {
    const fetchSpy = stubDefaultFetch(twoLocalAndRemote)
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1')
    await screen.findByTitle('切换工作空间')

    await user.click(screen.getByTitle('切换工作空间'))
    const dialog = await screen.findByRole('dialog', { name: '工作空间切换' })
    expect(within(dialog).queryByRole('button', { name: /远程 GPU/ })).toBeNull()
    const items = within(dialog)
      .getAllByRole('button')
      .filter((b) => b.className.includes('drawer-item'))
    expect(items.map((item) => item.textContent)).toEqual(['本机工作区', '第二本机'])

    const callsBefore = fetchSpy.mock.calls.length
    expect(fetchSpy.mock.calls.length).toBe(callsBefore)
    expect(screen.getByRole('dialog', { name: '工作空间切换' })).toBeInTheDocument()
  })

  it('Rail：remote workspace 不渲染（延期隐藏，不是 disabled 假入口）', async () => {
    stubDefaultFetch(twoLocalAndRemote)
    renderApp('/projects/p1/workspaces/w1')
    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => {
      expect(within(rail).getAllByText('本机工作区').length).toBeGreaterThan(0)
    })
    expect(within(rail).getByText('第二本机')).toBeInTheDocument()
    expect(within(rail).queryByText('远程 GPU')).toBeNull()
  })

  it('switcher roving keyboard：两个本机项 Arrow/Home/End/Enter 切换 selection，Esc 恢复焦点', async () => {
    stubDefaultFetch(twoLocalAndRemote)
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1')
    const trigger = await screen.findByTitle('切换工作空间')
    await waitFor(() => expect(trigger).toHaveTextContent('本机工作区'))
    await user.click(trigger)
    const dialog = await screen.findByRole('dialog', { name: '工作空间切换' })
    const items = within(dialog)
      .getAllByRole('button')
      .filter((b) => b.className.includes('drawer-item'))
    expect(items.length).toBe(2)

    ;(items[0] as HTMLElement).focus()
    await user.keyboard('{ArrowDown}')
    expect(items[1]).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(items[0]).toHaveFocus()
    await user.keyboard('{End}')
    expect(items[1]).toHaveFocus()
    await user.keyboard('{Home}')
    expect(items[0]).toHaveFocus()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '工作空间切换' })).not.toBeInTheDocument()
    })
    expect(trigger).toHaveFocus()
    expect(trigger).toHaveTextContent('本机工作区')

    await user.click(trigger)
    const again = await screen.findByRole('dialog', { name: '工作空间切换' })
    const againItems = within(again)
      .getAllByRole('button')
      .filter((b) => b.className.includes('drawer-item'))
    ;(againItems[0] as HTMLElement).focus()
    await user.keyboard('{ArrowDown}')
    expect(againItems[1]).toHaveFocus()
    await user.keyboard('{Enter}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '工作空间切换' })).not.toBeInTheDocument()
    })
    expect(await screen.findByRole('heading', { name: '第二本机' })).toBeInTheDocument()
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('第二本机')
    expect(screen.getByTitle('切换工作空间')).not.toHaveTextContent('本机工作区')
    expect(screen.queryByText('远程 GPU')).toBeNull()
  })
})
