import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { metaOk, REG_P1, workspaceW1 } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
const localTwo = { ...workspaceW1, workspace_id: 'w-local-2', name: '第二本机' }
const twoLocalStub = {
  [`/api/projects/${REG_P1}/workspaces/w1/work-items`]: emptyWorkItems,
  [`/api/projects/${REG_P1}/workspaces/w-local-2/work-items`]: emptyWorkItems,
  [`/api/project-registry/projects/${REG_P1}/workspaces`]: {
    data: { items: [workspaceW1, localTwo] },
    meta: metaOk,
  },
  [`/api/project-registry/projects/${REG_P1}/workspaces/w-local-2`]: {
    data: localTwo,
    meta: metaOk,
  },
}

describe('命令面板（Dialog 语义）', () => {
  it('切换项目打开抽屉、Esc 关闭、焦点恢复到触发按钮；命令面板不存在', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/projects')

    expect(screen.queryByRole('button', { name: /搜索或运行命令/ })).not.toBeInTheDocument()
    const trigger = await screen.findByTitle('切换项目')
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: '项目切换' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(screen.queryByRole('dialog', { name: '命令面板' })).not.toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '项目切换' })).not.toBeInTheDocument()
    })
    expect(trigger).toHaveFocus()
  })

  it('⌘K 键盘打开 → 不打开命令面板', async () => {
    stubDefaultFetch()
    renderApp('/projects')
    await screen.findByRole('button', { name: '添加项目' })
    fireEvent.keyDown(document, { key: 'k', metaKey: true })
    expect(screen.queryByRole('dialog', { name: '命令面板' })).not.toBeInTheDocument()
  })

  it('WorkspaceSwitcher ↑↓/Home/End/Enter 在两个本机项上导航并选中', async () => {
    stubDefaultFetch(twoLocalStub)
    const user = userEvent.setup()
    renderApp('/projects/p1/workspaces/w1')
    const trigger = await screen.findByTitle('切换工作空间')
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: '工作空间切换' })
    const items = within(dialog)
      .getAllByRole('button')
      .filter((b) => b.className.includes('drawer-item'))
    expect(items.map((item) => item.textContent)).toEqual(['本机工作区', '第二本机'])

    ;(items[0] as HTMLElement).focus()
    await user.keyboard('{ArrowDown}')
    expect(items[1]).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(items[0]).toHaveFocus()
    await user.keyboard('{End}')
    expect(items[1]).toHaveFocus()
    await user.keyboard('{Home}')
    expect(items[0]).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    await user.keyboard('{Enter}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '工作空间切换' })).not.toBeInTheDocument()
    })
    expect(await screen.findByRole('heading', { name: '第二本机' })).toBeInTheDocument()
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('第二本机')
    expect(screen.queryByRole('option', { name: /环境自检/ })).not.toBeInTheDocument()
  })

  it('输入筛选列表 → 无命令输入框；项目抽屉仍列出 Registry 名称', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/projects')
    await user.click(await screen.findByTitle('切换项目'))
    const dialog = await screen.findByRole('dialog', { name: '项目切换' })
    expect(screen.queryByLabelText('命令输入框')).not.toBeInTheDocument()
    expect(within(dialog).getByText('Alpha 项目')).toBeInTheDocument()
    expect(within(dialog).getByText('Project One')).toBeInTheDocument()
    await user.keyboard('doctor')
    expect(screen.queryByRole('option', { name: /环境自检/ })).not.toBeInTheDocument()
    expect(within(dialog).getByText('Alpha 项目')).toBeInTheDocument()
  })
})
