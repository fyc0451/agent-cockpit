import { render, screen, waitFor } from '@testing-library/react'
import { StatusState } from '../components/StatusState'
import { defaultFetchMap } from '../fixtures/api'
import { renderApp, stubFetch } from './helpers'

const RUNTIME_MINI = '[data-testid="runtime-mini"]'

// Bare legacy shapes for the 6 endpoints that WEB-004 switched to legacyGet.
// These match the real server.py response bodies (no G3 envelope).
const bareOverview = {
  projects: [],
  total_unread: 0,
  total_projects: 0,
  total_agents: 0,
  agent_mail: { available: true, reason: null },
}
const bareAttention = {
  sessions: [],
  items: [],
  count: 0,
  mail_unread: 0,
  capabilities: { agent_mail: { available: true, reason: null } },
}
const bareHerdr = { available: true, binary: '/usr/local/bin/herdr' }
const bareSettings = { language: 'zh', known_agents: ['claude'], languages: ['zh', 'en'] }

/** Override legacy endpoints in a defaultFetchMap with bare shapes */
function withLegacyOverrides(map: Record<string, unknown>): Record<string, unknown> {
  return {
    ...map,
    '/api/overview': bareOverview,
    '/api/attention': bareAttention,
    '/api/herdr/status': bareHerdr,
    '/api/settings': bareSettings,
  }
}

describe('页面级九态（P1-6）', () => {
  it('loading：overview 未落定 → loading 态；RuntimeMini 为 muted 而非 success', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview') || url.startsWith('/api/herdr/status')) {
        return new Promise(() => {}) as never // 永不落定，保持 loading
      }
      const map = withLegacyOverrides(defaultFetchMap())
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
    await waitFor(() => {
      expect(mini).toHaveAttribute('data-tone', 'muted')
    })
    expect(mini).not.toHaveAttribute('data-tone', 'success')
  })

  it('empty：真无数据（无 degraded）才显示 empty，且无 degraded banner', async () => {
    stubFetch(withLegacyOverrides(defaultFetchMap()))
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="empty"]')).toBeInTheDocument()
    })
    expect(screen.getByText('还没有可汇总的工作')).toBeInTheDocument()
    expect(container.querySelector('[data-state="degraded"]')).not.toBeInTheDocument()
  })

  it('partial-degraded：attention query 失败 → degraded banner + 有数据仍渲染', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/attention')) {
        return { status: 400, body: { detail: 'Herdr 超时' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('overview 失败 + attention 正常 → 保留 overview 降级', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview')) {
        return { status: 400, body: { detail: 'Registry 超时' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
  })

  it('两个 query 同时失败 → 整页 error（非 degraded）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview') || url.startsWith('/api/attention')) {
        return { status: 400, body: { detail: 'timeout' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      // Both queries failed → full page error (not degraded)
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
  })

  it('degraded 且无数据 → degraded 区块而非 empty', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/attention')) {
        return { status: 400, body: { detail: 'Herdr 超时' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.queryByText('还没有可汇总的工作')).not.toBeInTheDocument()
  })

  it('overview 失败 + attention 空数组 → degraded 而非 empty', async () => {
    stubFetch({
      ...withLegacyOverrides(defaultFetchMap()),
      '/api/overview': { status: 400, body: { detail: 'timeout' } },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('disconnected：transport_lost → disconnected 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/overview') || url.startsWith('/api/attention')) {
        return {
          status: 502,
          body: { error: { code: 'transport_lost', message: '连接中断', retryable: false } },
        }
      }
      const map = withLegacyOverrides(defaultFetchMap())
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

  it('stale：doctor 数据源返回 error → error 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/env-check')) {
        return { status: 400, body: { detail: '缓存过期' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    const { container } = renderApp('/settings?view=doctor')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
  })

  it('conflict：workbench 409 → conflict 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/projects/p1/workbench')) {
        return {
          status: 409,
          body: { error: { code: 'conflict', message: '版本冲突', retryable: false } },
        }
      }
      const map = withLegacyOverrides(defaultFetchMap())
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

  it('forbidden/unavailable：files.read 关闭 → forbidden 态（页面级）', async () => {
    stubFetch(withLegacyOverrides(defaultFetchMap()))
    const { container } = renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
  })

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
      const map = withLegacyOverrides(defaultFetchMap())
      const key = Object.keys(map)
        .filter((k) => url.startsWith(k))
        .sort((a, b) => b.length - a.length)[0]
      return key ? { body: map[key] } : undefined
    })
    return renderApp('/overview')
  }

  it('available=true → success', async () => {
    const { container } = miniTone({ body: bareHerdr })
    await waitFor(() => {
      expect(container.querySelector(RUNTIME_MINI)).toHaveAttribute('data-tone', 'success')
    })
  })

  it('available=false → danger', async () => {
    const { container } = miniTone({ body: { available: false, binary: '/usr/local/bin/herdr' } })
    await waitFor(() => {
      const mini = container.querySelector(RUNTIME_MINI)
      expect(mini).toHaveAttribute('data-tone', 'danger')
      expect(mini?.textContent).toContain('degraded')
      expect(mini?.textContent).toContain('本地 Herdr 二进制不可用')
    })
  })

  it('query error → danger degraded，不得 success', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/herdr/status')) {
        return { status: 400, body: { detail: 'Herdr 不可达' } }
      }
      const map = withLegacyOverrides(defaultFetchMap())
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
