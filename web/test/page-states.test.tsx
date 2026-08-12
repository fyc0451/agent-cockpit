import { render, screen, waitFor } from '@testing-library/react'
import { StatusState } from '../components/StatusState'
import { attentionPayload, defaultFetchMap, metaOk } from '../fixtures/api'
import { renderApp, stubFetch } from './helpers'

const RUNTIME_MINI = '[data-testid="runtime-mini"]'

describe('页面级九态（P1-6）', () => {
  it('loading：overview 未落定 → loading 态；RuntimeMini 为 muted 而非 success', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview') || url.startsWith('/api/herdr/status')) {
        return new Promise(() => {}) as never // 永不落定，保持 loading
      }
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="loading"]')).toBeInTheDocument()
    })
    const mini = container.querySelector(RUNTIME_MINI)
    // herdr 也未落定 → muted；负断言：不得显示 success
    await waitFor(() => {
      expect(mini).toHaveAttribute('data-tone', 'muted')
    })
    expect(mini).not.toHaveAttribute('data-tone', 'success')
  })

  it('empty：真无数据（sources 全 available）才显示 empty，且无 degraded banner', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/attention': { data: { items: [] }, meta: metaOk },
      '/api/overview': { data: { projects: [] }, meta: metaOk },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="empty"]')).toBeInTheDocument()
    })
    expect(screen.getByText('还没有可汇总的工作')).toBeInTheDocument()
    expect(container.querySelector('[data-state="degraded"]')).not.toBeInTheDocument()
  })

  it('partial-degraded：source failed → degraded banner + 区块 partial，不得出现 empty', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/attention': {
        data: attentionPayload.data,
        meta: {
          ...metaOk,
          sources: [{ name: 'herdr', status: 'failed', observed_at: null, reason: 'Herdr 超时' }],
        },
      },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(screen.getByText(/Herdr 超时/)).toBeInTheDocument()
    // 有数据的区块仍渲染，且全页无 empty、无 success tone
    expect(screen.getByText('ReviewPacket 待决定')).toBeInTheDocument()
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('degraded 且无数据 → degraded 区块而非 empty（banner 与 empty 不得同屏）', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/attention': {
        data: { items: [] },
        meta: {
          ...metaOk,
          sources: [{ name: 'herdr', status: 'failed', observed_at: null, reason: 'Herdr 超时' }],
        },
      },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.queryByText('还没有可汇总的工作')).not.toBeInTheDocument()
  })

  it('disconnected：transport_lost → disconnected 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview') || url.startsWith('/api/attention')) {
        return {
          status: 502,
          body: { error: { code: 'transport_lost', message: '连接中断', retryable: false } },
        }
      }
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="disconnected"]')).toBeInTheDocument()
    })
  })

  it('stale：doctor 数据源返回 data_stale → stale 态（页面级）', async () => {
    // env-check 用 error envelope code=data_stale
    stubFetch((url) => {
      if (url.startsWith('/api/env-check')) {
        return { body: { error: { code: 'data_stale', message: '缓存过期', retryable: false } } }
      }
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/settings?view=doctor')
    await waitFor(() => {
      expect(container.querySelector('[data-state="stale"]')).toBeInTheDocument()
    })
    expect(screen.getByText('缓存过期')).toBeInTheDocument()
  })

  it('conflict：workbench 409 → conflict 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/projects/p1/workbench')) {
        return {
          status: 409,
          body: { error: { code: 'conflict', message: '版本冲突', retryable: false } },
        }
      }
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/projects/p1/workbench')
    await waitFor(() => {
      expect(container.querySelector('[data-state="conflict"]')).toBeInTheDocument()
    })
  })

  it('forbidden/unavailable：files.read 关闭 → forbidden 态（页面级，详见 files.test）', async () => {
    stubFetch(defaultFetchMap())
    const { container } = renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
  })

  // W1 没有真写操作页面，operation-running / operation-partial-failure 无页面载体；
  // 以组件级 fixture 断言这两态的完整表达（data-state / 标题 / 色调），页面级预留。
  it('operation-running：组件表达（W1 无页面载体）', () => {
    const { container } = render(<StatusState kind="running" />)
    expect(container.querySelector('[data-state="running"]')).toBeInTheDocument()
    expect(screen.getByText('操作进行中…')).toBeInTheDocument()
    expect(container.querySelector('.state-spinner')).toBeInTheDocument()
  })

  it('operation-partial-failure：组件表达（W1 无页面载体）', () => {
    const { container } = render(<StatusState kind="partial-failure" />)
    expect(container.querySelector('[data-state="partial-failure"]')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})

describe('RuntimeMini 状态色调（P1-6）', () => {
  function miniTone(herdrSpec: { body: unknown; status?: number }) {
    stubFetch((url) => {
      if (url.startsWith('/api/herdr/status')) return herdrSpec
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    return renderApp('/overview')
  }

  it('healthy → success', async () => {
    const { container } = miniTone({ body: { data: { status: 'running', name: 'Herdr', healthy: true }, meta: metaOk } })
    await waitFor(() => {
      expect(container.querySelector(RUNTIME_MINI)).toHaveAttribute('data-tone', 'success')
    })
  })

  it('healthy=false → danger + 原因，不得 success', async () => {
    const { container } = miniTone({
      body: { data: { status: 'running', name: 'Herdr', healthy: false, message: 'session 丢失' }, meta: metaOk },
    })
    await waitFor(() => {
      const mini = container.querySelector(RUNTIME_MINI)
      expect(mini).toHaveAttribute('data-tone', 'danger')
      expect(mini?.textContent).toContain('session 丢失')
    })
  })

  it('source disconnected → danger + reason', async () => {
    const { container } = miniTone({
      body: {
        data: { status: 'running', name: 'Herdr', healthy: true },
        meta: {
          ...metaOk,
          sources: [{ name: 'herdr', status: 'unavailable', observed_at: null, reason: 'socket 断开' }],
        },
      },
    })
    await waitFor(() => {
      const mini = container.querySelector(RUNTIME_MINI)
      expect(mini).toHaveAttribute('data-tone', 'danger')
      expect(mini?.textContent).toContain('socket 断开')
    })
  })

  it('source stale → warning + observed_at', async () => {
    const { container } = miniTone({
      body: {
        data: { status: 'running', name: 'Herdr', healthy: true },
        meta: {
          ...metaOk,
          sources: [{ name: 'herdr', status: 'stale', observed_at: '2026-08-12T08:00:00Z', reason: null }],
        },
      },
    })
    await waitFor(() => {
      const mini = container.querySelector(RUNTIME_MINI)
      expect(mini).toHaveAttribute('data-tone', 'warning')
      expect(mini?.textContent).toContain('2026-08-12T08:00:00Z')
    })
  })

  it('query error → danger degraded，不得 success', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/herdr/status')) {
        return { status: 503, body: { error: { code: 'server_error', message: 'Herdr 不可达', retryable: false } } }
      }
      const map = defaultFetchMap()
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      const mini = container.querySelector(RUNTIME_MINI)
      expect(mini).toHaveAttribute('data-tone', 'danger')
      expect(mini?.textContent).toContain('degraded')
    })
  })
})
