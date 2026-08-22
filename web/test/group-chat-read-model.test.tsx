import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { defaultFetchMap } from '../fixtures/api'
import { renderApp, type MockResponseSpec } from './helpers'

const sessions = [
  { name: 'cockpit', status: 'running', directory: '/repo/cockpit', socket: '' },
  { name: 'platform', status: 'running', directory: '/repo/platform', socket: '' },
]

const panes = [
  {
    pane_id: 'w1:p1', session: 'cockpit', agent: 'grok', agent_status: 'idle',
    cwd: '/repo/cockpit', cwd_name: 'cockpit', display_name: 'BrownDesert',
    mail_name: 'BrownDesert', tab_id: 'w1:t1', focused: false,
  },
  {
    pane_id: 'w1:p2', session: 'platform', agent: 'codex', agent_status: 'idle',
    cwd: '/repo/platform', cwd_name: 'platform', display_name: 'DarkGlacier',
    mail_name: 'DarkGlacier', tab_id: 'w1:t2', focused: false,
  },
]

const workspaces = {
  workspaces: [{
    id: 'ws-1', path: '/repo', title: 'repo', created_at: '', order: 0,
    threads: [
      { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
      { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
    ],
  }],
  threads: [
    { id: 'th-1', workspace_id: 'ws-1', herdr_session: 'cockpit', title: 'cockpit', created_at: '' },
    { id: 'th-2', workspace_id: 'ws-1', herdr_session: 'platform', title: 'platform', created_at: '' },
  ],
}

function mappedResponse(url: string): MockResponseSpec | undefined {
  const map = {
    ...defaultFetchMap(),
    '/api/herdr/sessions': { sessions },
    '/api/herdr/snapshot': { panes },
    '/api/chat/workspaces': workspaces,
    '/api/agent-mail/config': { hub: '', team_hub: '', human_auth: '' },
  }
  const key = Object.keys(map)
    .filter((candidate) => url === candidate || url.startsWith(`${candidate}?`))
    .sort((a, b) => b.length - a.length)[0]
  return key ? { body: map[key as keyof typeof map] } : undefined
}

function stubAsyncFetch(
  special: (url: string) => MockResponseSpec | undefined | Promise<MockResponseSpec | undefined>,
) {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const spec = (await special(url)) ?? mappedResponse(url) ?? {
      status: 404,
      body: { error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } },
    }
    const status = spec.status ?? 200
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => spec.body,
    } as Response
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

describe('群聊 read-model 不变式', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('SSE 构造失败且 Hub 补充读取失败时，首屏账本历史仍保留', async () => {
    vi.stubGlobal('EventSource', class {
      constructor() {
        throw new Error('stream unavailable')
      }
    })
    const fetchMock = stubAsyncFetch((url) => {
      if (url === '/api/chat/sessions/cockpit/mail?source=ledger') {
        return {
          body: { messages: [{
            id: 'msg_keep', sender: 'human', program: '', text: '账本历史必须保留',
            to: ['BrownDesert'], thread: 'cockpit', ts: 1,
            source: 'composer', direct: true,
          }] },
        }
      }
      if (url === '/api/chat/sessions/cockpit/mail') {
        return { status: 503, body: { detail: 'Hub unavailable' } }
      }
      return undefined
    })

    renderApp('/chat?session=cockpit')
    expect(await screen.findByText('账本历史必须保留')).toBeInTheDocument()
    expect(screen.getByText('composer')).toHaveClass('gc-src-badge')
    expect(screen.getByText('定向')).toHaveClass('gc-direct-badge')
    expect(screen.queryByText(/邮件历史读失败/)).not.toBeInTheDocument()
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/mail-status'))).toBe(true)
    })
    expect(screen.getByText('账本历史必须保留')).toBeInTheDocument()
  })

  it('旧会话历史迟到时只写旧 query key，不污染已切换的新会话', async () => {
    const user = userEvent.setup()
    let releaseCockpit: ((value: MockResponseSpec) => void) | undefined
    const cockpitLedger = new Promise<MockResponseSpec>((resolve) => {
      releaseCockpit = resolve
    })
    stubAsyncFetch(async (url) => {
      if (url === '/api/chat/sessions/cockpit/mail?source=ledger') return cockpitLedger
      if (url === '/api/chat/sessions/platform/mail?source=ledger') {
        return { body: { messages: [{
          id: 'msg_platform', sender: 'human', program: '', text: '新会话历史',
          to: ['DarkGlacier'], thread: 'platform', ts: 2,
        }] } }
      }
      if (url === '/api/chat/sessions/platform/mail') return { body: { messages: [] } }
      if (url === '/api/chat/sessions/cockpit/mail') return { body: { messages: [] } }
      return undefined
    })

    renderApp('/chat?session=cockpit')
    await screen.findByText('platform')
    await user.click(screen.getByText('platform'))
    expect(await screen.findByText('新会话历史')).toBeInTheDocument()

    await act(async () => {
      releaseCockpit?.({ body: { messages: [{
        id: 'msg_cockpit', sender: 'human', program: '', text: '旧会话迟到历史',
        to: ['BrownDesert'], thread: 'cockpit', ts: 1,
      }] } })
      await cockpitLedger
    })

    await waitFor(() => {
      expect(screen.getByText('platform', { selector: '.gc-toolbar-title' })).toBeInTheDocument()
    })
    expect(screen.getByText('新会话历史')).toBeInTheDocument()
    expect(screen.queryByText('旧会话迟到历史')).not.toBeInTheDocument()
  })
})
