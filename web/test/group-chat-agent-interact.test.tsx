import { act, fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import {
  AgentInteractModal,
  mergePaneOutput,
  paneWatchInterval,
} from '../features/group-chat/AgentInteractModal'

const mocks = vi.hoisted(() => ({
  fetchPaneOutput: vi.fn(),
  sendPane: vi.fn(),
}))

vi.mock('../api/auth', () => ({
  requireAuthenticated: vi.fn(async () => {}),
}))

vi.mock('../api/legacyHerdr', () => ({
  fetchPaneOutput: (...args: unknown[]) => mocks.fetchPaneOutput(...args),
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
    vi.useFakeTimers()
    mocks.fetchPaneOutput.mockReset()
    mocks.sendPane.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('合并轮询快照的重叠行，保留可回滚历史', () => {
    expect(mergePaneOutput('one\ntwo\nthree', 'two\nthree\nfour')).toBe(
      'one\ntwo\nthree\nfour',
    )
    expect(mergePaneOutput('one', 'two')).toBe('one\ntwo')
    const bounded = mergePaneOutput('old-one\nold-two', 'fresh-one\nfresh-two', 16)
    expect(bounded.length).toBeLessThanOrEqual(16)
    expect(bounded).toMatch(/fresh-two$/)
  })

  it('工作时 400ms 轮询，空闲放慢', () => {
    expect(paneWatchInterval('working')).toBe(400)
    expect(paneWatchInterval('blocked')).toBe(400)
    expect(paneWatchInterval('idle')).toBe(2000)
  })

  it('工作时更快把新快照追加成可聚焦的只读 log', async () => {
    mocks.fetchPaneOutput
      .mockResolvedValueOnce({ output: 'one\ntwo\nthree', error: null })
      .mockResolvedValueOnce({ output: 'two\nthree\nfour', error: null })

    render(
      <AgentInteractModal member={member} session="cockpit" onClose={vi.fn()} />,
    )

    await act(async () => {})
    const log = screen.getByRole('log', { name: '只读终端现场' })
    expect(log).toHaveTextContent('one two three')
    expect(log).toHaveAttribute('tabindex', '0')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })

    expect(log).toHaveTextContent('one two three four')
    expect(mocks.fetchPaneOutput).toHaveBeenCalledTimes(2)
    expect(mocks.fetchPaneOutput).toHaveBeenLastCalledWith('cockpit', 'w1:p2', 200)
  })

  it('用户向上滚动复盘时，新快照不抢回底部', async () => {
    mocks.fetchPaneOutput
      .mockResolvedValueOnce({ output: 'one\ntwo\nthree', error: null })
      .mockResolvedValueOnce({ output: 'two\nthree\nfour', error: null })

    render(
      <AgentInteractModal member={member} session="cockpit" onClose={vi.fn()} />,
    )
    await act(async () => {})

    const log = screen.getByRole('log', { name: '只读终端现场' })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 200 })
    log.scrollTop = 120
    fireEvent.scroll(log)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })

    expect(log.scrollTop).toBe(120)
    expect(log).toHaveTextContent('four')
  })
})
