import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { MemberPanel } from '../features/group-chat/MemberPanel'

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

  it('群成员显示各自 Agent 的矢量图标', () => {
    render(
      <MemberPanel
        {...baseProps}
        members={['codex', 'claude', 'kimi', 'opencode', 'grok'].map(member)}
        session="demo-1"
        onOpenTerminal={vi.fn()}
      />,
    )

    for (const kind of ['codex', 'claude', 'kimi', 'opencode', 'grok']) {
      expect(document.querySelector(`[data-agent-icon="${kind}"]`)).toBeInTheDocument()
    }
    expect(screen.queryByText('🤖')).not.toBeInTheDocument()
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
})
