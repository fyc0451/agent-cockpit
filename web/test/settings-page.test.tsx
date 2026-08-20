import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubDefaultFetch } from './helpers'

const versionPayload = {
  current: { version: '0.3.6' },
  latest: { version: '0.3.7', name: '0.3.7', url: 'https://github.com/fyc0451/agent-cockpit/releases/tag/agent-cockpit-v0.3.7' },
  status: 'update_available',
  checked_at: '2026-08-20T00:00:00Z',
}

const upgradeIdle = {
  job_id: null,
  state: 'idle',
  engine: 'source-checkout',
  target_version: null,
  from_version: null,
  phase: null,
  error_code: null,
  error_message: null,
  active: false,
  available: true,
  reason: null,
}

describe('设置挂在 3.0 外壳', () => {
  beforeEach(() => {
    stubDefaultFetch({
      '/api/version': versionPayload,
      '/api/upgrade/status': upgradeIdle,
    })
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

  it('升级页显示当前版本和一键升级', async () => {
    renderApp('/settings?view=upgrade')
    expect(await screen.findByRole('tab', { name: '升级' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText(/当前 0.3.6/)).toBeInTheDocument()
    expect(screen.getByText(/发现 0.3.7/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '一键升级' })).not.toHaveAttribute('aria-disabled', 'true')
  })
})
