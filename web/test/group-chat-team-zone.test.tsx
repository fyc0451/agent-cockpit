import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

  it('话题已绑定本机 Session 时显示绑定状态', () => {
    renderWithAppFrame(
      <WorkspaceBrowser
        {...baseProps}
        teamEnabled={true}
        teamLoggedIn={true}
        teamUsername="test-user"
        teamTopics={[{ slug: 'proj-a', name: '项目 A', id: 1 }]}
        teamBindings={[{ project_slug: 'proj-a', session: 'local-session-1' }]}
        onTeamLogout={vi.fn()}
        onTeamBindSession={vi.fn()}
        onTeamSelectTopic={vi.fn()}
      />,
    )
    expect(screen.getByText(/local-session-1/)).toBeInTheDocument()
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
