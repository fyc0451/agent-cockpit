import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
})
