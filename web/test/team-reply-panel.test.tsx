import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TeamReplyPanel } from '../features/team/TeamReplyPanel'

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const binding = {
  project_slug: 'ready',
  session: 'hr-ready-3',
  active: true,
  ready: true,
  replyMode: 'confirm' as const,
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('TeamReplyPanel', () => {
  it('展示 pending 草稿，批准后从列表移除', async () => {
    let pending = true
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/reply-drafts') && (init?.method ?? 'GET') === 'GET') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            drafts: pending ? [{
              id: 12,
              inbox_item_id: 31,
              subject: '请确认回复',
              body_md: '已经处理完成',
              importance: 'normal',
              mention_handles: ['alice'],
              status: 'pending',
              message_id: null,
              created_at: '2026-08-23 10:00:00',
              updated_at: '2026-08-23 10:00:00',
              decided_at: null,
            }] : [],
          }),
        } as Response
      }
      if (url.endsWith('/reply-drafts/12/approve') && init?.method === 'POST') {
        pending = false
        return { ok: true, status: 201 } as Response
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TeamReplyPanel topic="ready" binding={binding} />, { wrapper })

    expect(await screen.findByText('请确认回复')).toBeInTheDocument()
    expect(screen.getByText('已经处理完成')).toBeInTheDocument()
    expect(screen.getByText(/@alice/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: '确认发送' }))
    await waitFor(() => expect(screen.getByText('暂无待确认草稿')).toBeInTheDocument())
  })

  it('启用自动回复前明确确认并调用本机模式切换端点', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/reply-drafts')) {
        return { ok: true, status: 200, json: async () => ({ drafts: [] }) } as Response
      }
      if (url.endsWith('/reply-mode') && init?.method === 'PATCH') {
        return { ok: true, status: 200 } as Response
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TeamReplyPanel topic="ready" binding={binding} />, { wrapper })
    await screen.findByText('暂无待确认草稿')
    await userEvent.setup().click(screen.getByRole('button', { name: '自动回复' }))

    expect(confirm).toHaveBeenCalledOnce()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/team-auth/session-bindings/ready/reply-mode',
      expect.objectContaining({ method: 'PATCH' }),
    ))
  })

  it('可拒绝草稿且不会调用批准端点', async () => {
    let pending = true
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/reply-drafts')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ drafts: pending ? [{
            id: 13,
            inbox_item_id: 32,
            subject: '不发送',
            body_md: '草稿正文',
            importance: 'normal',
            mention_handles: ['alice'],
            status: 'pending',
            message_id: null,
            created_at: '',
            updated_at: '',
            decided_at: null,
          }] : [] }),
        } as Response
      }
      if (url.endsWith('/reply-drafts/13/reject') && init?.method === 'POST') {
        pending = false
        return { ok: true, status: 200 } as Response
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TeamReplyPanel topic="ready" binding={binding} />, { wrapper })
    await screen.findByText('不发送')
    await userEvent.setup().click(screen.getByRole('button', { name: '拒绝' }))

    await waitFor(() => expect(screen.getByText('暂无待确认草稿')).toBeInTheDocument())
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/approve'))).toBe(false)
  })
})
