import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubDefaultFetch } from './helpers'

describe('设置挂在 3.0 外壳', () => {
  beforeEach(() => {
    stubDefaultFetch()
  })

  it('默认外观，没有返回群聊链接，也没有 Harness 页', async () => {
    renderApp('/settings')
    expect(await screen.findByRole('tab', { name: '外观' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('radio', { name: '跟随系统' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '设置' })).toBeInTheDocument()
    expect(screen.queryByText('返回群聊')).not.toBeInTheDocument()
    expect(screen.queryByText(/Harness/)).not.toBeInTheDocument()
  })

  it('?view=doctor 打开环境自检', async () => {
    renderApp('/settings?view=doctor')
    expect(await screen.findByRole('tab', { name: '环境自检' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('Herdr 未运行')).toBeInTheDocument()
    expect(screen.getByText('agent_mail')).toBeInTheDocument()
  })

  it('侧栏点设置进入 /settings，点会话回到群聊', async () => {
    const user = userEvent.setup()
    renderApp('/chat')
    await user.click(await screen.findByRole('button', { name: '设置' }))
    expect(await screen.findByRole('tab', { name: '外观' })).toBeInTheDocument()
    expect(screen.queryByText('返回群聊')).not.toBeInTheDocument()
  })
})
