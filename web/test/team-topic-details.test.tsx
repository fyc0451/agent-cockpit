import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { DetailsPanel } from '../features/group-chat/DetailsPanel'
import { TEAM_BINDINGS_REFRESH_MS } from '../features/group-chat/GroupChatPage'
import { AppFrame } from '../features/shell/AppFrame'
import { renderApp, stubDefaultFetch } from './helpers'

function renderTeamDetails({
  loading = false,
  error = null,
}: {
  loading?: boolean
  error?: string | null
} = {}) {
  return render(
    <AppFrame sidebar={null}>
      <DetailsPanel
        tab="members"
        onTabChange={vi.fn()}
        members={[]}
        session="agent-cockpit-1"
        workdir="/repo"
        onMention={vi.fn()}
        onFilter={vi.fn()}
        onInteract={vi.fn()}
        onOpenTerminal={vi.fn()}
        onMembersChanged={vi.fn()}
        fileRoot="/repo"
        onPreview={vi.fn()}
        teamTopic="hr-ready"
        teamMembers={[]}
        teamMembersLoading={loading}
        teamMembersError={error}
      />
    </AppFrame>,
  )
}

describe('团队话题成员详情', () => {
  it('登录后每 5 秒刷新 Session 绑定候选', () => {
    expect(TEAM_BINDINGS_REFRESH_MS).toBe(5_000)
  })

  it('只显示 Team Hub 已加入的同事，不显示本机 Agent 或 Boss', () => {
    render(
      <AppFrame sidebar={null}>
        <DetailsPanel
          tab="members"
          onTabChange={vi.fn()}
          members={[]}
          session="agent-cockpit-1"
          workdir="/repo"
          onMention={vi.fn()}
          onFilter={vi.fn()}
          onInteract={vi.fn()}
          onOpenTerminal={vi.fn()}
          onMembersChanged={vi.fn()}
          fileRoot="/repo"
          onPreview={vi.fn()}
          teamTopic="hr-ready"
          teamMembers={[
            {
              human_id: 7,
              display_name: 'Alice Chen',
              mention_handle: 'alice',
              role: 'admin',
              status: 'active',
            },
            {
              human_id: 8,
              display_name: 'Pending User',
              mention_handle: 'pending',
              role: 'member',
              status: 'invited',
            },
          ]}
        />
      </AppFrame>,
    )

    const panel = screen.getByLabelText('团队成员')
    expect(within(panel).getByText('Alice Chen')).toBeInTheDocument()
    expect(within(panel).getByText('@alice · 管理员')).toBeInTheDocument()
    expect(within(panel).queryByText('Pending User')).not.toBeInTheDocument()
    expect(within(panel).queryByText('我')).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '文件' })).not.toBeInTheDocument()
  })

  it('打开团队话题时从 Team Hub 成员接口取数', async () => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    const fetchMock = stubDefaultFetch({
      '/api/herdr/sessions': {
        sessions: [{ name: 'agent-cockpit-1', status: 'running', directory: '/repo', socket: '' }],
      },
      '/api/herdr/snapshot': {
        panes: [{
          pane_id: 'w1:p2',
          session: 'agent-cockpit-1',
          agent: 'codex',
          agent_status: 'idle',
          cwd: '/repo',
          cwd_name: 'repo',
          display_name: 'TopazOwl',
          mail_name: 'TopazOwl',
          tab_id: 'w1:p2',
          focused: false,
        }],
      },
      '/api/chat/workspaces': {
        workspaces: [{
          id: 'ws-1',
          path: '/repo',
          title: 'agent-cockpit',
          created_at: '',
          order: 0,
          threads: [{
            id: 'th-1',
            workspace_id: 'ws-1',
            herdr_session: 'agent-cockpit-1',
            title: 'agent-cockpit-1',
            created_at: '',
          }],
        }],
        threads: [{
          id: 'th-1',
          workspace_id: 'ws-1',
          herdr_session: 'agent-cockpit-1',
          title: 'agent-cockpit-1',
          created_at: '',
        }],
      },
      '/api/chat/sessions/agent-cockpit-1/mail': { messages: [] },
      '/api/agent-mail/config': {
        hub: '',
        team_hub: 'https://team.example',
        human_auth: 'https://auth.example',
      },
      '/api/team-auth/status': { logged_in: true, username: 'fyc', roles: ['admin'] },
      '/api/team-auth/session-bindings': {
        sessions: [],
        bindings: [{ project_slug: 'hr-ready', session: 'agent-cockpit-1' }],
        topics: [],
      },
      '/api/team/projects': { projects: [{ slug: 'hr-ready', name: 'HR Ready', id: 1 }] },
      '/api/team/projects/hr-ready/members': {
        members: [{
          human_id: 7,
          display_name: 'Alice Chen',
          mention_handle: 'alice',
          role: 'member',
          status: 'active',
        }],
      },
      '/api/team/ledger/messages': { messages: [] },
    })

    renderApp('/chat?session=agent-cockpit-1')
    const topic = await screen.findByTitle('打开 HR Ready（绑定到 agent-cockpit-1）')
    await userEvent.setup().click(topic)

    const panel = await screen.findByLabelText('团队成员')
    expect(within(panel).getByText('Alice Chen')).toBeInTheDocument()
    expect(within(panel).queryByText('TopazOwl')).not.toBeInTheDocument()
    expect(within(panel).queryByText('我')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/team/projects/hr-ready/members',
      { credentials: 'include' },
    )
  })

  it('成员接口加载中显示明确状态', () => {
    renderTeamDetails({ loading: true })
    expect(screen.getByText('成员加载中…')).toBeInTheDocument()
  })

  it('没有已加入同事时显示空状态', () => {
    renderTeamDetails()
    expect(screen.getByText('该话题暂无成员')).toBeInTheDocument()
  })

  it('成员接口失败时显示错误且不回退本机成员', () => {
    renderTeamDetails({ error: '读取成员列表失败' })
    const panel = screen.getByLabelText('团队成员')
    expect(within(panel).getByText('读取成员列表失败')).toBeInTheDocument()
    expect(within(panel).queryByText('我')).not.toBeInTheDocument()
  })
})
