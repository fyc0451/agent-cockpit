import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as teamLedger from '../api/teamLedger'
import { TeamTimeline } from '../features/team/TeamTimeline'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

function message(id: number, body_md: string, subject = '群聊消息') {
  return {
    id,
    subject,
    body_md,
    mention_handles: [],
    importance: 'normal',
    created_ts: '2026-08-22 12:00:00',
    sender_name: '付彦超',
    sender_human_id: 1,
    sender_kind: 'session_lead',
    sender_agent: 'codex-main',
  }
}

describe('TeamTimeline', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.useRealTimers())

  it('发送和历史只走远端 Team Hub，不写本机群账本', async () => {
    const listed: Array<Record<string, unknown>> = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/team/projects/proj-a/chat/messages' && method === 'GET') {
        return { ok: true, status: 200, json: async () => ({ messages: listed }) } as Response
      }
      if (url === '/api/team/projects/proj-a/support-requests' && method === 'POST') {
        const body = JSON.parse(String(init?.body))
        listed.push(message(1, body.body_md))
        return { ok: true, status: 201, json: async () => ({ status: 'delivered' }) } as Response
      }
      throw new Error(`unexpected fetch ${url} ${method}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<TeamTimeline topic="proj-a" topicName="项目 A" />, { wrapper: createWrapper() })

    expect(await screen.findByText(/还没有团队消息/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('团队消息'), 'hello team')
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('hello team')).toBeInTheDocument()
    expect(screen.getByText(/付彦超 · via codex-main/)).toBeInTheDocument()
    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(urls).toContain('/api/team/projects/proj-a/chat/messages')
    expect(urls).toContain('/api/team/projects/proj-a/support-requests')
    expect(urls.some((url) => url.includes('/api/team/ledger') || url.includes('/api/chat'))).toBe(false)
  })

  it('每两秒自动拉取新团队回复', async () => {
    vi.useFakeTimers()
    let poll = 0
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ messages: poll++ === 0 ? [] : [message(2, '自动出现的回复')] }),
    } as Response)))

    render(<TeamTimeline topic="proj-a" topicName="项目 A" />, { wrapper: createWrapper() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText(/还没有团队消息/)).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    expect(screen.getByText('自动出现的回复')).toBeInTheDocument()
  })

  it('首次打开团队话题直接定位到最新消息', async () => {
    const originalScrollHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype, 'scrollHeight',
    )
    const originalClientHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype, 'clientHeight',
    )
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('gc-team-timeline-list') ? 2400 : 0
      },
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('gc-team-timeline-list') ? 400 : 0
      },
    })
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        messages: Array.from({ length: 12 }, (_, index) => message(index + 1, `第 ${index + 1} 条`)),
      }),
    } as Response)))

    try {
      const { container } = render(
        <TeamTimeline topic="proj-a" topicName="项目 A" />,
        { wrapper: createWrapper() },
      )
      expect(await screen.findByText('第 12 条')).toBeInTheDocument()
      const list = container.querySelector('.gc-team-timeline-list') as HTMLElement
      expect(list.scrollTop).toBe(2400)
    } finally {
      if (originalScrollHeight) {
        Object.defineProperty(HTMLElement.prototype, 'scrollHeight', originalScrollHeight)
      }
      if (originalClientHeight) {
        Object.defineProperty(HTMLElement.prototype, 'clientHeight', originalClientHeight)
      }
    }
  })

  it('用户上翻历史后不抢位置，并可一键回到最新消息', async () => {
    vi.useFakeTimers()
    const height = { current: 1200 }
    const originalScrollHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype, 'scrollHeight',
    )
    const originalClientHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype, 'clientHeight',
    )
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('gc-team-timeline-list') ? height.current : 0
      },
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains('gc-team-timeline-list') ? 400 : 0
      },
    })
    let poll = 0
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        messages: poll++ === 0
          ? [message(1, '旧消息')]
          : [message(1, '旧消息'), message(2, '新消息')],
      }),
    } as Response)))

    try {
      const { container } = render(
        <TeamTimeline topic="proj-a" topicName="项目 A" />,
        { wrapper: createWrapper() },
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByText('旧消息')).toBeInTheDocument()
      const list = container.querySelector('.gc-team-timeline-list') as HTMLElement
      list.scrollTop = 100
      fireEvent.scroll(list)
      height.current = 2000
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
      expect(screen.getByText('新消息')).toBeInTheDocument()
      expect(list.scrollTop).toBe(100)
      fireEvent.click(screen.getByRole('button', { name: '↓ 有新消息' }))
      expect(list.scrollTop).toBe(2000)
    } finally {
      if (originalScrollHeight) {
        Object.defineProperty(HTMLElement.prototype, 'scrollHeight', originalScrollHeight)
      }
      if (originalClientHeight) {
        Object.defineProperty(HTMLElement.prototype, 'clientHeight', originalClientHeight)
      }
    }
  })

  it('在对应消息下先确认，授权后才进入 Lead 处理', async () => {
    let approved = false
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/chat/messages') && method === 'GET') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ messages: [message(42, '请处理这条问题')] }),
        } as Response
      }
      if (url.endsWith('/reply-requests') && method === 'GET') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            requests: [{
              inbox_item_id: 31,
              message_id: 42,
              status: approved ? 'queued' : 'awaiting_confirmation',
              decision: approved ? 'approved' : null,
              decided_at: approved ? '2026-08-23 12:01:00' : null,
            }],
          }),
        } as Response
      }
      if (url.endsWith('/reply-requests/31/approve') && method === 'POST') {
        approved = true
        return { ok: true, status: 201 } as Response
      }
      throw new Error(`unexpected fetch ${url} ${method}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      <TeamTimeline
        topic="proj-a"
        topicName="项目 A"
        binding={{
          project_slug: 'proj-a',
          session: 'demo',
          ready: true,
          replyMode: 'confirm',
        }}
      />,
      { wrapper: createWrapper() },
    )

    expect(await screen.findByText('请处理这条问题')).toBeInTheDocument()
    expect(await screen.findByText('是否让 Lead 回复这条消息？确认前不会生成答案。')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '让 Lead 回复' }))
    expect(await screen.findByText('已允许回复，等待 Lead 处理…')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).endsWith('/reply-requests/31/approve')
    ))).toBe(true)
  })

  it('自动回复模式不显示旧的逐条确认按钮', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/chat/messages')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ messages: [message(43, '自动处理这条问题')] }),
        } as Response
      }
      if (url.endsWith('/reply-requests')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            requests: [{
              inbox_item_id: 32,
              message_id: 43,
              status: 'awaiting_confirmation',
              decision: null,
              decided_at: null,
            }],
          }),
        } as Response
      }
      throw new Error(`unexpected fetch ${url}`)
    }))

    render(
      <TeamTimeline
        topic="proj-a"
        topicName="项目 A"
        binding={{
          project_slug: 'proj-a',
          session: 'demo',
          ready: true,
          replyMode: 'auto',
        }}
      />,
      { wrapper: createWrapper() },
    )

    expect(await screen.findByText('自动处理这条问题')).toBeInTheDocument()
    expect(await screen.findByText('自动回复已启用，等待 Lead 处理…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '让 Lead 回复' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '不回复' })).not.toBeInTheDocument()
  })

  it('回复默认显示原问题前 20 个字，展开后显示完整问答', async () => {
    const question = '数据库连接失败后应该如何排查连接池、网络配置和服务状态？'
    const preview = `${Array.from(question).slice(0, 20).join('')}…`
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        messages: [
          message(50, question, '数据库连接失败'),
          message(51, '已确认收到。稍后给出排查步骤。', 'Re: 数据库连接失败'),
        ],
      }),
    } as Response)))

    const user = userEvent.setup()
    render(<TeamTimeline topic="proj-a" topicName="项目 A" />, { wrapper: createWrapper() })

    const summary = await screen.findByText(preview)
    const details = summary.closest('details')
    expect(details).not.toHaveAttribute('open')
    expect(summary).not.toHaveTextContent('已确认收到')

    await user.click(summary)
    expect(details).toHaveAttribute('open')
    expect(screen.getByText('完整提问')).toBeInTheDocument()
    expect(screen.getAllByText(question)).toHaveLength(2)
    expect(screen.getByText('回复详情')).toBeInTheDocument()
    expect(screen.getByText('已确认收到。稍后给出排查步骤。')).toBeInTheDocument()
  })

  it('找不到原问题时保留回复正文', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        messages: [message(60, '没有匹配原问题的回复', 'Re: 已过期问题')],
      }),
    } as Response)))

    render(<TeamTimeline topic="proj-a" topicName="项目 A" />, { wrapper: createWrapper() })

    const reply = await screen.findByText('没有匹配原问题的回复')
    expect(reply.closest('details')).toBeNull()
  })

  it('API 只暴露 Team Hub 群聊读写', () => {
    expect(Object.keys(teamLedger).sort()).toEqual(['listTeamMessages', 'sendTeamMessage'])
  })
})
