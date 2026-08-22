import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WorkspaceBrowser } from '../features/shell/WorkspaceBrowser'
import { AppFrame } from '../features/shell/AppFrame'

function renderWithAppFrame(ui: React.ReactElement) {
  return render(
    <AppFrame sidebar={null}>
      {ui}
    </AppFrame>
  )
}

describe('WorkspaceBrowser 团队区域', () => {
  const baseProps = {
    groups: [],
    ungrouped: [],
    activeSession: null,
    loading: false,
    wide: true,
    onSelect: vi.fn(),
    onAddWorkspace: vi.fn(),
    onNewSession: vi.fn(),
    onRemoveWorkspace: vi.fn(),
    onStopSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onOpenWorkspace: vi.fn(),
  }

  it('未配置 Team Hub 时不显示团队区域', () => {
    renderWithAppFrame(<WorkspaceBrowser {...baseProps} teamEnabled={false} />)
    expect(screen.queryByText('团队')).not.toBeInTheDocument()
  })

  it('配置 Team Hub 但未登录时显示登录按钮', () => {
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={false}
        onTeamLogin={vi.fn()}
      />,
    )
    expect(screen.getByText('团队')).toBeInTheDocument()
    expect(screen.getByText('登录团队账号')).toBeInTheDocument()
    expect(screen.getByText('邀请码注册')).toBeInTheDocument()
  })

  it('邀请码注册覆盖缺失邀请码与 pending 提示', async () => {
    const onTeamRegister = vi.fn().mockResolvedValue(undefined)
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={false}
        onTeamRegister={onTeamRegister}
      />,
    )
    const user = userEvent.setup()
    await user.click(screen.getByText('邀请码注册'))
    await user.type(screen.getByLabelText('注册用户名'), 'alice')
    await user.type(screen.getByLabelText('注册显示名'), 'Alice Chen')
    await user.type(screen.getByLabelText('注册密码'), 'password-1234')
    await user.type(screen.getByLabelText('确认注册密码'), 'password-1234')
    await user.click(screen.getByText('提交注册申请'))
    expect(screen.getByText('请填写邀请码')).toBeInTheDocument()
    expect(onTeamRegister).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('团队邀请码'), 'INVITE-123')
    await user.click(screen.getByText('提交注册申请'))
    await waitFor(() => expect(onTeamRegister).toHaveBeenCalledWith({
      username: 'alice',
      displayName: 'Alice Chen',
      password: 'password-1234',
      inviteCode: 'INVITE-123',
    }))
    expect(screen.getByText(/账号 alice 已提交，当前为待批准/)).toBeInTheDocument()
  })

  it('邀请链接自动打开注册并填入邀请码，不要求成员手抄', async () => {
    window.location.hash = '#/chat?team_invite=INVITE-LINK&team_project=ready'
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={false}
        onTeamRegister={vi.fn()}
      />,
    )
    expect(await screen.findByText('受邀加入 ready')).toBeInTheDocument()
    expect(screen.getByLabelText('团队邀请码')).toHaveValue('INVITE-LINK')
    window.location.hash = ''
  })

  it('登录后显示用户名和话题列表', () => {
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={true}
        teamUsername="test-user"
        teamTopics={[
          { slug: 'proj-a', name: '项目 A', id: 1 },
          { slug: 'proj-b', name: '项目 B', id: 2 },
        ]}
        teamBindings={[]}
        onTeamLogout={vi.fn()}
        onTeamBindSession={vi.fn()}
        onTeamSelectTopic={vi.fn()}
      />,
    )
    expect(screen.getByText(/test-user/)).toBeInTheDocument()
    expect(screen.getByText('项目 A')).toBeInTheDocument()
    expect(screen.getByText('项目 B')).toBeInTheDocument()
  })

  it('话题已绑定已停止的 Session 时显示失效状态和改绑入口', () => {
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={true}
        teamUsername="test-user"
        teamTopics={[{ slug: 'proj-a', name: '项目 A', id: 1 }]}
        teamBindings={[{ project_slug: 'proj-a', session: 'local-session-1', active: false }]}
        onTeamLogout={vi.fn()}
        onTeamBindSession={vi.fn()}
        onTeamSelectTopic={vi.fn()}
      />,
    )
    expect(screen.getByText(/local-session-1（已停止）/)).toBeInTheDocument()
    expect(screen.getByTitle('更换本机 Session')).toHaveTextContent('改绑')
  })

  it('改绑候选使用 Team 后端列表，不依赖工作区树分组', async () => {
    const onTeamBindSession = vi.fn().mockResolvedValue(undefined)
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={true}
        teamUsername="test-user"
        teamTopics={[{ slug: 'ready', name: 'hr-ready', id: 1 }]}
        teamBindings={[{ project_slug: 'ready', session: 'hr-ready', active: false, projectRef: 'project-ready' }]}
        teamSessions={[
          {
            name: 'hr-ready-team',
            label: 'hr-ready-team · Lead GoldRiver',
            status: 'idle',
            agentCount: 1,
            ready: true,
            reason: null,
            leadName: 'GoldRiver',
            projectRef: 'project-ready',
          },
          {
            name: 'agent-cockpit-1',
            label: 'agent-cockpit-1 · Lead TopazOwl',
            status: 'idle',
            agentCount: 1,
            ready: true,
            reason: null,
            leadName: 'TopazOwl',
            projectRef: 'project-cockpit',
          },
        ]}
        onTeamLogout={vi.fn()}
        onTeamBindSession={onTeamBindSession}
        onTeamSelectTopic={vi.fn()}
      />,
    )

    const user = userEvent.setup()
    await user.click(screen.getByTitle('更换本机 Session'))
    expect(screen.queryByText('agent-cockpit-1 · Lead TopazOwl')).not.toBeInTheDocument()
    await user.click(screen.getByText('hr-ready-team · Lead GoldRiver'))
    await waitFor(() => expect(onTeamBindSession).toHaveBeenCalledWith('ready', 'hr-ready-team'))
  })

  it('未加入可申请，invited 等待审批，active 审批刷新后可绑定', async () => {
    const onTeamJoin = vi.fn().mockResolvedValue(undefined)
    const props = {
      ...baseProps,
      teamEnabled: true,
      teamLoggedIn: true,
      teamUsername: 'test-user',
      teamBindings: [],
      onTeamLogout: vi.fn(),
      onTeamJoin,
      onTeamBindSession: vi.fn(),
      onTeamSelectTopic: vi.fn(),
    }
    const { rerender } = renderWithAppFrame(
      <WorkspaceBrowser
        {...props}
        teamTopics={[
          { slug: 'proj-a', name: '项目 A', id: 1, membership: null },
          { slug: 'proj-b', name: '项目 B', id: 2, membership: { role: 'member', status: 'invited', mention_handle: 'waiting' } },
        ]}
      />,
    )
    const user = userEvent.setup()
    expect(screen.getByTitle('项目 B（加入申请等待审批）')).toBeInTheDocument()
    await user.click(screen.getByTitle('申请加入 项目 A'))
    expect(screen.getByLabelText('项目 A @花名')).toHaveValue('test-user')
    await user.click(screen.getByText('提交申请'))
    await waitFor(() => expect(onTeamJoin).toHaveBeenCalledWith('proj-a', 'test-user'))

    rerender(
      <AppFrame sidebar={null}>
        <WorkspaceBrowser
          {...props}
          teamTopics={[
            { slug: 'proj-a', name: '项目 A', id: 1, membership: { role: 'member', status: 'active', mention_handle: 'test-user' } },
            { slug: 'proj-b', name: '项目 B', id: 2, membership: { role: 'member', status: 'invited', mention_handle: 'waiting' } },
          ]}
        />
      </AppFrame>,
    )
    expect(screen.getByTitle('项目 A（需要先绑定本机 Session）')).toBeInTheDocument()
    expect(screen.queryByTitle('申请加入 项目 A')).not.toBeInTheDocument()
  })

  it('新建 topic 已移到团队管理页，侧栏只保留消息相关功能', () => {
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={true}
        teamUsername="fyc"
        teamTopics={[]}
        teamBindings={[]}
        onTeamLogout={vi.fn()}
      />,
    )
    expect(screen.queryByText('新建 topic')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('topic 名称')).not.toBeInTheDocument()
  })
})

describe('WorkspaceBrowser 工作区导航', () => {
  it('点工作区标题只打开，不把会话列表收起', async () => {
    const onOpen = vi.fn()
    const onSelect = vi.fn()
    renderWithAppFrame(
      <WorkspaceBrowser
        groups={[{
          id: 'ws-1',
          root: '/repo',
          label: 'agent-cockpit',
          removable: true,
          rows: [{ name: 'cockpit', status: 'idle', memberCount: 2, root: '/repo' }],
        }]}
        ungrouped={[]}
        activeSession="cockpit"
        loading={false}
        wide={true}
        onSelect={onSelect}
        onAddWorkspace={vi.fn()}
        onNewSession={vi.fn()}
        onRemoveWorkspace={vi.fn()}
        onStopSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onOpenWorkspace={onOpen}
      />,
    )
    expect(screen.getByText('cockpit')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByText('agent-cockpit'))
    expect(onOpen).toHaveBeenCalledWith('ws-1')
    expect(screen.getByText('cockpit')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByText('cockpit'))
    expect(onSelect).toHaveBeenCalledWith('cockpit')
  })
})


describe('WorkspaceBrowser 团队区管理入口', () => {
  const entryProps = {
    groups: [],
    ungrouped: [],
    activeSession: null,
    loading: false,
    wide: true,
    onSelect: vi.fn(),
    onAddWorkspace: vi.fn(),
    onNewSession: vi.fn(),
    onRemoveWorkspace: vi.fn(),
    onStopSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onOpenWorkspace: vi.fn(),
    teamEnabled: true,
    teamLoggedIn: true,
    teamUsername: 'fyc',
    teamTopics: [{ slug: 'proj-a', name: '项目 A', id: 1 }],
    teamBindings: [],
    onTeamLogout: vi.fn(),
    onTeamBindSession: vi.fn(),
    onTeamSelectTopic: vi.fn(),
  }

  it('管理员看到团队管理入口，点击跳转；审批功能不在侧栏', async () => {
    const onOpenTeamAdmin = vi.fn()
    renderWithAppFrame(
      <WorkspaceBrowser {...entryProps} teamIsAdmin={true} onOpenTeamAdmin={onOpenTeamAdmin} />,
    )
    // 审批/账号管理不在侧栏
    expect(screen.queryByText('账号管理（管理员）')).not.toBeInTheDocument()
    expect(screen.queryByText('成员管理 ▾')).not.toBeInTheDocument()
    const entry = screen.getByText('团队管理（账号 / 成员审批）→')
    await userEvent.setup().click(entry)
    expect(onOpenTeamAdmin).toHaveBeenCalled()
  })

  it('非管理员看不到团队管理入口', () => {
    renderWithAppFrame(<WorkspaceBrowser {...entryProps} teamIsAdmin={false} />)
    expect(screen.queryByText('团队管理（账号 / 成员审批）→')).not.toBeInTheDocument()
  })
})
