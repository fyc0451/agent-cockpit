import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { renderApp, stubDefaultFetch } from './helpers'

const pane = (
  session: string,
  paneId: string,
  agent: string,
  name: string,
) => ({
  pane_id: paneId,
  session,
  agent,
  agent_status: 'idle',
  cwd: `/repo/${session}`,
  cwd_name: session,
  display_name: name,
  mail_name: name,
  tab_id: paneId,
  focused: false,
})

describe('点左侧工作区会话', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    stubDefaultFetch({
      '/api/herdr/sessions': {
        sessions: [
          { name: 'cockpit', status: 'running', directory: '/repo/cockpit', socket: '' },
          { name: 'platform', status: 'running', directory: '/repo/platform', socket: '' },
        ],
      },
      '/api/herdr/snapshot': {
        panes: [
          pane('cockpit', 'w1:p1', 'grok', 'BrownDesert'),
          pane('cockpit', 'w1:p7', 'claude', 'BlueElk'),
          pane('platform', 'w1:p2', 'codex', 'DarkGlacier'),
        ],
      },
      '/api/chat/workspaces': {
        workspaces: [{
          id: 'ws-1',
          path: '/repo',
          title: 'agent-cockpit',
          created_at: '',
          order: 0,
          threads: [
            { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
            { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
          ],
        }],
        threads: [
          { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
          { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
        ],
      },
      '/api/chat/sessions/cockpit/mail': { messages: [] },
      '/api/chat/sessions/platform/mail': { messages: [] },
      '/api/agent-mail/config': { hub: '', team_hub: '', human_auth: '' },
    })
  })

  it('点另一个会话立刻出主栏，不卸成白屏', async () => {
    const user = userEvent.setup()
    renderApp('/chat?session=cockpit')
    expect(await screen.findByText('platform')).toBeInTheDocument()
    expect(screen.getByText('开始群聊')).toBeInTheDocument()
    await user.click(screen.getByText('platform'))
    await waitFor(() => {
      expect(screen.getByText('platform', { selector: '.gc-toolbar-title' })).toBeInTheDocument()
    })
    expect(screen.getByText('开始群聊')).toBeInTheDocument()
    expect(screen.queryByText('Minified React error')).not.toBeInTheDocument()
  })

  it('从团队管理页点左侧会话会返回群聊主栏', async () => {
    const user = userEvent.setup()
    renderApp('/team?session=cockpit')
    expect(await screen.findByText('团队管理', { selector: '.gc-toolbar-title' }))
      .toBeInTheDocument()

    await user.click(await screen.findByText('platform'))

    await waitFor(() => {
      expect(screen.getByText('platform', { selector: '.gc-toolbar-title' }))
        .toBeInTheDocument()
    })
    expect(screen.getByText('开始群聊')).toBeInTheDocument()
  })

  it('发送中切换会话，气泡不落到新会话（修复 onSend 竞态）', async () => {
    const user = userEvent.setup()
    window.sessionStorage.setItem('gc:draft:cockpit', 'cockpit draft')
    let resolveSend: (value: { mail_error?: string }) => void
    const sendPromise = new Promise<{ mail_error?: string }>((resolve) => {
      resolveSend = resolve
    })

    stubDefaultFetch({
      '/api/herdr/sessions': {
        sessions: [
          { name: 'cockpit', status: 'running', directory: '/repo/cockpit', socket: '' },
          { name: 'platform', status: 'running', directory: '/repo/platform', socket: '' },
        ],
      },
      '/api/herdr/snapshot': {
        panes: [
          pane('cockpit', 'w1:p1', 'grok', 'BrownDesert'),
          pane('platform', 'w1:p2', 'codex', 'DarkGlacier'),
        ],
      },
      '/api/chat/workspaces': {
        workspaces: [{
          id: 'ws-1',
          path: '/repo',
          title: 'agent-cockpit',
          created_at: '',
          order: 0,
          threads: [
            { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
            { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
          ],
        }],
        threads: [
          { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
          { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
        ],
      },
      '/api/chat/sessions/cockpit/mail': { messages: [] },
      '/api/agent-mail/config': { hub: '', team_hub: '', human_auth: '' },
      '/api/chat/sessions/platform/mail': () => sendPromise,
    })

    renderApp('/chat?session=platform')
    await screen.findByText('platform', { selector: '.gc-toolbar-title' })
    await screen.findByText('DarkGlacier')

    const textarea = screen.getByPlaceholderText('@leader 分派任务；+ 添加附件 / Skill；可粘贴截图')
    await user.type(textarea, '→DarkGlacier test message')
    const sendButton = screen.getByRole('button', { name: /排队发送|立刻打断发送/ })
    await user.click(sendButton)

    // 发送中立即切换到 cockpit 会话
    await user.click(screen.getByText('cockpit'))
    await waitFor(() => {
      expect(screen.getByText('cockpit', { selector: '.gc-toolbar-title' })).toBeInTheDocument()
    })
    expect(screen.getByPlaceholderText('@leader 分派任务；+ 添加附件 / Skill；可粘贴截图'))
      .toHaveValue('cockpit draft')

    // resolve 发送请求
    resolveSend!({})
    await vi.waitFor(() => Promise.resolve())

    // 验证：cockpit 会话的瀑布流中不应该出现 platform 的消息气泡
    expect(screen.queryByText('→DarkGlacier test message')).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText('@leader 分派任务；+ 添加附件 / Skill；可粘贴截图'))
      .toHaveValue('cockpit draft')
    // 注意：不能用 queryByText('排队') 因为 Composer 中也有"排队"按钮
    // 只要消息文本不在，就说明气泡没有落进来
  })

  const lastMailPost = () => {
    const calls = vi.mocked(window.fetch).mock.calls.filter(
      ([url, init]) =>
        String(url).includes('/api/chat/sessions/cockpit/mail')
        && (init as RequestInit | undefined)?.method === 'POST',
    )
    const post = calls[calls.length - 1]
    return post ? JSON.parse(String((post[1] as RequestInit).body)) : null
  }

  it('@某人发送带 direct=true，@all 广播不带（40 号方案发送侧）', async () => {
    const user = userEvent.setup()
    renderApp('/chat?session=cockpit')
    await screen.findByText('cockpit', { selector: '.gc-toolbar-title' })
    await screen.findByText('BlueElk')

    const textarea = screen.getByPlaceholderText('@leader 分派任务；+ 添加附件 / Skill；可粘贴截图')
    const sendButton = () => screen.getByRole('button', { name: /排队发送|立刻打断发送/ })

    await user.type(textarea, '@BlueElk 定向验收')
    await user.click(sendButton())
    await waitFor(() => {
      const body = lastMailPost()
      expect(body).not.toBeNull()
      expect(body.to).toEqual(['BlueElk'])
      expect(body.direct).toBe(true)
      expect(body.source).toBe('composer')
    })

    await user.type(textarea, '@all 广播验收')
    await user.click(sendButton())
    await waitFor(() => {
      const body = lastMailPost()
      expect(body).not.toBeNull()
      // 广播存 all 标记，由后端投递时展开全员；不带 direct
      expect(body.to).toEqual(['all'])
      expect(body.direct).toBeUndefined()
      expect(body.source).toBe('composer')
    })
  })
})
