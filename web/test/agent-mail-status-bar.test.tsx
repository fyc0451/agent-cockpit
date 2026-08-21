import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentMailStatusBar } from '../features/group-chat/AgentMailStatusBar'
import * as chatSessionApi from '../api/chatSession'
import type { ChatMember } from '../features/group-chat/model'

// Mock API
vi.mock('../api/chatSession', () => ({
  fetchAgentMailStatus: vi.fn(),
}))

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

function member(partial: Partial<ChatMember> & Pick<ChatMember, 'name' | 'paneId'>): ChatMember {
  return {
    session: 'cockpit',
    kind: 'codex',
    mailName: partial.name,
    cwd: '/tmp',
    isLeader: false,
    status: 'idle',
    ...partial,
  }
}

describe('AgentMailStatusBar', () => {
  it('不显示任何内容当所有 agent 都已连接', async () => {
    const members: ChatMember[] = [
      member({ name: 'human', paneId: 'w1:p0' }),
      member({ name: 'GrayFalcon', paneId: 'w1:p6' }),
    ]

    vi.mocked(chatSessionApi.fetchAgentMailStatus).mockResolvedValue({
      connected: true,
      pane_id: 'w1:p6',
      details: {
        has_mail_name: true,
        has_config_path: true,
        has_agent_session: true,
        can_send_mail: true,
      },
    })

    const { container } = render(
      <AgentMailStatusBar session="cockpit" members={members} />,
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(container.firstChild).toBeNull()
    })
  })

  it('显示警告当 agent 未连接', async () => {
    const members: ChatMember[] = [
      member({ name: 'human', paneId: 'w1:p0' }),
      member({ name: 'GrayFalcon', paneId: 'w1:p6' }),
    ]

    vi.mocked(chatSessionApi.fetchAgentMailStatus).mockResolvedValue({
      connected: false,
      pane_id: 'w1:p6',
      details: {
        has_mail_name: false,
        has_config_path: false,
        has_agent_session: false,
        can_send_mail: false,
      },
    })

    render(<AgentMailStatusBar session="cockpit" members={members} />, {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
      // GrayFalcon 出现多次（主文本 + 详情），使用 getAllByText
      expect(screen.getAllByText(/GrayFalcon/).length).toBeGreaterThan(0)
      expect(screen.getByText(/未连接到 Agent Mail/)).toBeInTheDocument()
    })
  })

  it('显示多个未连接的 agent', async () => {
    const members: ChatMember[] = [
      member({ name: 'GrayFalcon', paneId: 'w1:p5' }),
      member({ name: 'BrownDesert', paneId: 'w1:p8' }),
    ]

    vi.mocked(chatSessionApi.fetchAgentMailStatus).mockImplementation(
      async (_session: string, paneId: string) => ({
        connected: false,
        pane_id: paneId,
        details: {
          has_mail_name: false,
          has_config_path: true,
          has_agent_session: paneId === 'w1:p5', // p5 有，p8 没有
          can_send_mail: false,
        },
      }),
    )

    render(<AgentMailStatusBar session="cockpit" members={members} />, {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(screen.getByText(/2 个 agent/)).toBeInTheDocument()
      expect(screen.getByText(/未连接到 Agent Mail/)).toBeInTheDocument()
    })
  })

  it('换会话成员人数变了也不卸页', async () => {
    const first: ChatMember[] = [
      member({ name: 'GrayFalcon', paneId: 'w1:p6' }),
    ]
    const second: ChatMember[] = [
      member({ name: 'GrayFalcon', paneId: 'w1:p6' }),
      member({ name: 'BrownDesert', paneId: 'w1:p1' }),
      member({ name: 'BlueElk', paneId: 'w1:p7' }),
    ]
    vi.mocked(chatSessionApi.fetchAgentMailStatus).mockResolvedValue({
      connected: true,
      pane_id: 'w1:p6',
      details: {
        has_mail_name: true,
        has_config_path: true,
        has_agent_session: true,
        can_send_mail: true,
      },
    })

    const { rerender } = render(
      <AgentMailStatusBar session="cockpit" members={first} />,
      { wrapper: createWrapper() },
    )
    expect(() => {
      rerender(<AgentMailStatusBar session="other" members={second} />)
      rerender(<AgentMailStatusBar session="other" members={[]} />)
    }).not.toThrow()
  })

  it('排除 human 和没有 paneId 的成员', async () => {
    const members: ChatMember[] = [
      member({ name: 'human', paneId: 'w1:p0' }),
      member({ name: 'GrayFalcon', paneId: '' }),
    ]

    // 清除之前测试的调用记录
    vi.mocked(chatSessionApi.fetchAgentMailStatus).mockClear()

    const { container } = render(
      <AgentMailStatusBar session="cockpit" members={members} />,
      { wrapper: createWrapper() },
    )

    // 不应该调用 API（因为没有有效的 agent paneId）
    await waitFor(() => {
      expect(chatSessionApi.fetchAgentMailStatus).not.toHaveBeenCalled()
      expect(container.firstChild).toBeNull()
    })
  })
})
