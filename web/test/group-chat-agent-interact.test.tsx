import { act, fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import {
  AgentInteractModal,
  mergePaneOutput,
} from '../features/group-chat/AgentInteractModal'

const mocks = vi.hoisted(() => ({
  sendPane: vi.fn(),
  liveUrl: 'ws://localhost/api/chat/sessions/cockpit/panes/w1%3Ap2/live',
}))

class FakeWebSocket {
  static instance: FakeWebSocket | null = null
  static OPEN = 1
  readyState = FakeWebSocket.OPEN
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instance = this
  }
  close() {
    if (FakeWebSocket.instance === this) FakeWebSocket.instance = null
  }
}

vi.mock('../api/auth', () => ({
  requireAuthenticated: vi.fn(async () => {}),
}))

vi.mock('../api/legacyHerdr', () => ({
  paneLiveWebSocketUrl: () => mocks.liveUrl,
  sendPane: (...args: unknown[]) => mocks.sendPane(...args),
}))

const member = {
  paneId: 'w1:p2',
  session: 'cockpit',
  kind: 'codex',
  name: 'CalmMarsh',
  mailName: 'CalmMarsh',
  status: 'working',
  cwd: '/repo',
  isLeader: false,
}

describe('AgentInteractModal 只读现场流', () => {
  beforeEach(() => {
    mocks.sendPane.mockReset()
    vi.stubGlobal('WebSocket', FakeWebSocket)
    FakeWebSocket.instance = null
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('合并快照的重叠行，保留可回滚历史', () => {
    expect(mergePaneOutput('one\ntwo\nthree', 'two\nthree\nfour')).toBe(
      'one\ntwo\nthree\nfour',
    )
    expect(mergePaneOutput('one', 'two')).toBe('one\ntwo')
    const bounded = mergePaneOutput('old-one\nold-two', 'fresh-one\nfresh-two', 16)
    expect(bounded.length).toBeLessThanOrEqual(16)
    expect(bounded).toMatch(/fresh-two$/)
  })

  it('用 WebSocket 快照追加成可聚焦的只读 log，不按 400ms 轮询', async () => {
    render(
      <AgentInteractModal member={member} session="cockpit" onClose={vi.fn()} />,
    )
    const socket = FakeWebSocket.instance
    expect(socket?.url).toBe(mocks.liveUrl)

    await act(async () => {
      socket?.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', output: 'one\ntwo\nthree', error: null }),
      })
    })
    const log = screen.getByRole('log', { name: '只读终端现场' })
    expect(log).toHaveTextContent('one two three')
    expect(log).toHaveAttribute('tabindex', '0')

    await act(async () => {
      socket?.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', output: 'two\nthree\nfour', error: null }),
      })
    })
    expect(log).toHaveTextContent('one two three four')
  })

  it('用户向上滚动复盘时，新快照不抢回底部', async () => {
    render(
      <AgentInteractModal member={member} session="cockpit" onClose={vi.fn()} />,
    )
    const socket = FakeWebSocket.instance
    await act(async () => {
      socket?.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', output: 'one\ntwo\nthree', error: null }),
      })
    })

    const log = screen.getByRole('log', { name: '只读终端现场' })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 200 })
    log.scrollTop = 120
    fireEvent.scroll(log)

    await act(async () => {
      socket?.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', output: 'two\nthree\nfour', error: null }),
      })
    })

    expect(log.scrollTop).toBe(120)
    expect(log).toHaveTextContent('four')
  })

  it('现场窗更大，Esc 和关闭都能关掉', () => {
    const onClose = vi.fn()
    render(<AgentInteractModal member={member} session="cockpit" onClose={onClose} />)
    expect(document.querySelector('.gc-modal--live')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '关闭现场' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
