import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { MemberPanel } from '../features/group-chat/MemberPanel'

vi.mock('../api/legacyHerdr', async () => {
  const actual = await vi.importActual<typeof import('../api/legacyHerdr')>('../api/legacyHerdr')
  return {
    ...actual,
    closePane: vi.fn(),
    restartPane: vi.fn(),
    startAgent: vi.fn(),
  }
})

import { closePane, restartPane } from '../api/legacyHerdr'

const baseProps = {
  members: [],
  workdir: '/repo',
  open: true,
  onMention: vi.fn(),
  onFilter: vi.fn(),
  onInteract: vi.fn(),
  onChanged: vi.fn(),
}

function member(kind: string) {
  return {
    paneId: `%${kind}`,
    session: 'demo-1',
    kind,
    name: `${kind}-member`,
    mailName: `${kind}-member`,
    status: 'idle',
    cwd: '/repo',
    isLeader: false,
  }
}

describe('MemberPanel Herdr 终端入口', () => {
  it('有当前会话时从群成员标题栏打开 Herdr 终端', () => {
    const onOpenTerminal = vi.fn()
    render(
      <MemberPanel
        {...baseProps}
        session="demo-1"
        onOpenTerminal={onOpenTerminal}
      />,
    )

    const button = screen.getByRole('button', { name: '打开 Herdr 终端' })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(onOpenTerminal).toHaveBeenCalledOnce()
  })

  it('快照还没到时显示成员加载中，不装成没人', () => {
    render(
      <MemberPanel
        {...baseProps}
        session="demo-1"
        loading
        onOpenTerminal={vi.fn()}
      />,
    )
    expect(screen.getByText('成员加载中…')).toBeInTheDocument()
  })

  it('没有当前会话时禁用终端入口', () => {
    render(
      <MemberPanel
        {...baseProps}
        session={null}
        onOpenTerminal={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: '打开 Herdr 终端' })).toBeDisabled()
  })

  it('添加成员可选 qodercli', () => {
    render(
      <MemberPanel
        {...baseProps}
        session="demo-1"
        onOpenTerminal={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '＋' }))
    expect(screen.getByRole('radio', { name: /qodercli/ })).toBeInTheDocument()
  })

  it('群成员显示各自 Agent 的矢量图标', () => {
    render(
      <MemberPanel
        {...baseProps}
        members={['codex', 'claude', 'kimi', 'opencode', 'grok', 'qodercli'].map(member)}
        session="demo-1"
        onOpenTerminal={vi.fn()}
      />,
    )

    for (const kind of ['codex', 'claude', 'kimi', 'opencode', 'grok', 'qodercli']) {
      expect(document.querySelector(`[data-agent-icon="${kind}"]`)).toBeInTheDocument()
    }
    expect(screen.queryByText('🤖')).not.toBeInTheDocument()
  })

  it('blocked 成员状态写成等你输入，操作是处理', () => {
    const onInteract = vi.fn()
    const onOpenTerminal = vi.fn()
    render(
      <MemberPanel
        {...baseProps}
        members={[{ ...member('codex'), status: 'blocked' }]}
        session="demo-1"
        onOpenTerminal={onOpenTerminal}
        onInteract={onInteract}
      />,
    )
    expect(screen.getByText(/等你输入/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '处理' }))
    expect(onInteract).toHaveBeenCalledOnce()
    expect(onOpenTerminal).not.toHaveBeenCalled()
  })

  it('空闲成员的终端按钮打开 Herdr 终端，不走文本 dump', () => {
    const onOpenTerminal = vi.fn()
    const onInteract = vi.fn()
    render(
      <MemberPanel
        {...baseProps}
        members={[member('codex')]}
        session="demo-1"
        onOpenTerminal={onOpenTerminal}
        onInteract={onInteract}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '终端' }))
    expect(onOpenTerminal).toHaveBeenCalledOnce()
    expect(onInteract).not.toHaveBeenCalled()
  })

  it('成员关闭走 DELETE pane，成功后刷新成员', async () => {
    const onChanged = vi.fn()
    vi.mocked(closePane).mockResolvedValue({ closed: '%codex' })
    window.confirm = vi.fn(() => true)
    render(
      <MemberPanel
        {...baseProps}
        members={[member('codex')]}
        session="demo-1"
        onOpenTerminal={vi.fn()}
        onChanged={onChanged}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '关闭 codex-member' }))
    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => {
      expect(closePane).toHaveBeenCalledWith('demo-1', '%codex')
      expect(onChanged).toHaveBeenCalledOnce()
    })
  })

  it('成员重启走 POST restart，成功后刷新成员', async () => {
    const onChanged = vi.fn()
    vi.mocked(restartPane).mockResolvedValue({ pane_id: '%codex' })
    window.confirm = vi.fn(() => true)
    render(
      <MemberPanel
        {...baseProps}
        members={[member('codex')]}
        session="demo-1"
        onOpenTerminal={vi.fn()}
        onChanged={onChanged}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '重启 codex-member' }))
    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => {
      expect(restartPane).toHaveBeenCalledWith('demo-1', '%codex')
      expect(onChanged).toHaveBeenCalledOnce()
    })
  })

  it('重启和关闭按钮都在成员操作区，桌面靠悬停露出', () => {
    const { container } = render(
      <MemberPanel
        {...baseProps}
        members={[member('codex')]}
        session="demo-1"
        onOpenTerminal={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '重启 codex-member' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭 codex-member' })).toBeInTheDocument()
    expect(container.querySelector('.gc-member-ops')).not.toBeNull()
    expect(container.querySelector('.gc-member-op--extra')).toBeNull()
  })

  it('成员有未读时露出条数', () => {
    render(
      <MemberPanel
        {...baseProps}
        members={[{ ...member('grok'), unread: 2 }]}
        session="demo-1"
        onOpenTerminal={vi.fn()}
      />,
    )
    expect(screen.getByTitle('@grok-member · 空闲 · 2 条未读')).toBeInTheDocument()
    expect(document.querySelector('.gc-unread-badge')?.textContent).toBe('2')
  })
})
