import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TeamTimeline } from '../features/team/TeamTimeline'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

describe('TeamTimeline', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('发一条看见一条，只打 /api/team/ledger*', async () => {
    const listed: Array<Record<string, unknown>> = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/team/ledger/messages') && (!init || init.method === 'GET' || !init.method)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ messages: listed }),
        } as Response
      }
      if (url === '/api/team/ledger/messages' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        const row = {
          id: 'tmsg_1',
          topic: body.topic,
          hub: '',
          kind: 'me',
          sender: 'human',
          text: body.text,
          to: [],
          ts: 1,
        }
        listed.push(row)
        return {
          ok: true,
          status: 200,
          json: async () => ({ ok: true, message: row }),
        } as Response
      }
      throw new Error(`unexpected fetch ${url} ${init?.method ?? 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<TeamTimeline topic="proj-a" topicName="项目 A" />, {
      wrapper: createWrapper(),
    })

    expect(await screen.findByText(/还没有团队消息/)).toBeInTheDocument()
    expect(screen.getByText(/消息只待团队账本，不进本机群/)).toBeInTheDocument()

    await user.type(screen.getByLabelText('团队消息'), 'hello team')
    await user.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('hello team')).toBeInTheDocument()
    expect(screen.getByTestId('team-msg-tmsg_1')).toBeInTheDocument()

    const urls = fetchMock.mock.calls.map(([input, init]) => ({
      url: String(input),
      method: (init as RequestInit | undefined)?.method ?? 'GET',
    }))
    expect(urls.every((call) => call.url.includes('/api/team/ledger'))).toBe(true)
    expect(urls.some((call) => call.url.includes('/api/chat') || call.url.includes('pane'))).toBe(false)
  })

  it('源码不碰本机群发送路径', () => {
    const files = [
      join(root, 'api/teamLedger.ts'),
      join(root, 'features/team/TeamTimeline.tsx'),
    ]
    for (const path of files) {
      const src = readFileSync(path, 'utf8')
      expect(src).not.toMatch(/from ['"].*chatSession['"]/)
      expect(src).not.toMatch(/\bsendSessionMail\b/)
      expect(src).not.toMatch(/\bpane_send\b/)
      expect(src).not.toMatch(/chat-messages\.json/)
    }
  })
})
