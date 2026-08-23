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
  it('明确说明确认前不会生成答案', () => {
    render(<TeamReplyPanel topic="ready" binding={binding} />, { wrapper })

    expect(screen.getByText('每条消息先问我')).toBeInTheDocument()
    expect(screen.getByText(/确认前不会生成答案/)).toBeInTheDocument()
    expect(screen.queryByText(/草稿/)).not.toBeInTheDocument()
  })

  it('启用自动回复前明确确认并调用本机模式切换端点', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 } as Response)
    vi.stubGlobal('fetch', fetchMock)

    render(<TeamReplyPanel topic="ready" binding={binding} />, { wrapper })
    await userEvent.setup().click(screen.getByRole('button', { name: '自动回复' }))

    expect(confirm).toHaveBeenCalledOnce()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/team-auth/session-bindings/ready/reply-mode',
      expect.objectContaining({ method: 'PATCH' }),
    ))
  })
})
