import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubDefaultFetch } from './helpers'

describe('命令面板（Dialog 语义）', () => {
  it('点击按钮打开、Esc 关闭、焦点恢复到触发按钮', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/overview')

    const trigger = await screen.findByRole('button', { name: /搜索或运行命令/ })
    await user.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: '命令面板' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // autofocus：输入框获得焦点
    expect(screen.getByLabelText('命令输入框')).toHaveFocus()
    // 底部注明服务端搜索未接通
    expect(screen.getByText(/搜索暂未接通，可继续用页面导航查找/)).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '命令面板' })).not.toBeInTheDocument()
    })
    expect(trigger).toHaveFocus()
  })

  it('⌘K 键盘打开', async () => {
    stubDefaultFetch()
    renderApp('/overview')
    await screen.findByRole('button', { name: /搜索或运行命令/ })
    fireEvent.keyDown(document, { key: 'k', metaKey: true })
    expect(await screen.findByRole('dialog', { name: '命令面板' })).toBeInTheDocument()
  })

  it('列出静态路由，↑↓/↵ 键盘导航', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/overview')
    const trigger = await screen.findByRole('button', { name: /搜索或运行命令/ })
    await user.click(trigger)

    // 静态路由列表
    expect(await screen.findByRole('option', { name: /项目列表/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /环境自检/ })).toBeInTheDocument()

    // ↓ 移动到第二项（项目列表），↵ 跳转
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('option', { name: /项目列表/ })).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{Enter}')

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '命令面板' })).not.toBeInTheDocument()
    })
    // 已导航到项目列表页
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
  })

  it('输入筛选列表', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    renderApp('/overview')
    await user.click(await screen.findByRole('button', { name: /搜索或运行命令/ }))
    await user.type(screen.getByLabelText('命令输入框'), 'doctor')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /环境自检/ })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: /项目列表/ })).not.toBeInTheDocument()
    })
  })
})
