import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { WorkspaceBrowser } from '../features/shell/WorkspaceBrowser'
import { AppFrame } from '../features/shell/AppFrame'

function renderWithAppFrame(ui: React.ReactElement) {
  return render(
    <AppFrame>
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
})
