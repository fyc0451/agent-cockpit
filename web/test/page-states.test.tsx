import { render, screen, waitFor } from '@testing-library/react'
import { StatusState } from '../components/StatusState'
import { defaultFetchMap, metaOk, REG_P1, registryProjectsEmptyPayload } from '../fixtures/api'
import { renderApp, stubFetch } from './helpers'

const RUNTIME_MINI = '[data-testid="runtime-mini"]'
const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }

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

function withLegacyOverrides(map: Record<string, unknown>): Record<string, unknown> {
  return {
    ...map,
    '/api/overview': bareOverview,
    '/api/attention': bareAttention,
    '/api/herdr/status': bareHerdr,
    '/api/settings': bareSettings,
    [WORK_ITEMS]: emptyWorkItems,
  }
}

describe('页面级九态（P1-6）', () => {
  it('loading：overview 未落定 → 项目列表 loading；无 RuntimeMini', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects')) {
        return new Promise(() => {}) as never
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
    expect(screen.getByText('正在加载项目列表…')).toBeInTheDocument()
    expect(container.querySelector(RUNTIME_MINI)).toBeNull()
  })

  it('empty：真无数据（无 degraded）才显示 empty，且无 degraded banner', async () => {
    stubFetch({
      ...withLegacyOverrides(defaultFetchMap()),
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="empty"]')).toBeInTheDocument()
    })
    expect(screen.getByText('还没有项目')).toBeInTheDocument()
    expect(screen.queryByText('还没有可汇总的工作')).not.toBeInTheDocument()
    expect(container.querySelector('[data-state="degraded"]')).not.toBeInTheDocument()
  })

  it('partial-degraded：attention query 失败 → 改为项目列表 degraded，不渲染 Attention', async () => {
    stubFetch({
      ...withLegacyOverrides(defaultFetchMap()),
      '/api/project-registry/projects': {
        data: { items: [], next_cursor: null },
        meta: { ...metaOk, partial: true },
      },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.queryByText('还没有可汇总的工作')).not.toBeInTheDocument()
  })

  it('overview 失败 + attention 正常 → 项目列表 typed error，无 RuntimeMini', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects')) {
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
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(container.querySelector(RUNTIME_MINI)).toBeNull()
  })

  it('两个 query 同时失败 → 整页 error（非 degraded）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects') || url.startsWith('/api/attention')) {
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
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="degraded"]')).not.toBeInTheDocument()
  })

  it('degraded 且无数据 → degraded 区块而非 empty', async () => {
    stubFetch({
      ...withLegacyOverrides(defaultFetchMap()),
      '/api/project-registry/projects': {
        data: { items: [], next_cursor: null },
        meta: { ...metaOk, partial: true },
      },
    })
    const { container } = renderApp('/overview')
    await waitFor(() => {
      expect(container.querySelector('[data-state="degraded"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.queryByText('还没有可汇总的工作')).not.toBeInTheDocument()
    expect(screen.queryByText('还没有项目')).not.toBeInTheDocument()
  })

  it('overview 失败 + attention 空数组 → error 而非 empty', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects')) {
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
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
  })

  it('disconnected：transport_lost → disconnected 态（页面级）', async () => {
    stubFetch((url) => {
      if (url.startsWith('/api/project-registry/projects')) {
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

  it('stale：doctor 数据源返回 error → /settings 回到项目列表，不渲染 Doctor', async () => {
    stubFetch(withLegacyOverrides(defaultFetchMap()))
    const { container } = renderApp('/settings?view=doctor')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(container.querySelector('[data-state="error"]')).not.toBeInTheDocument()
    expect(screen.queryByText('环境自检')).not.toBeInTheDocument()
  })

  it('conflict：workspace 列表 409 → conflict 态（页面级）', async () => {
    stubFetch((url) => {
      if (url === `/api/project-registry/projects/${REG_P1}/workspaces`) {
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
  function renderProjects() {
    stubFetch(withLegacyOverrides(defaultFetchMap()))
    return renderApp('/overview')
  }

  it('available=true → success → RuntimeMini 隐藏', async () => {
    const { container } = renderProjects()
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(container.querySelector(RUNTIME_MINI)).toBeNull()
  })

  it('available=false → danger → RuntimeMini 隐藏，无 Herdr 文案', async () => {
    const { container } = renderProjects()
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(container.querySelector(RUNTIME_MINI)).toBeNull()
    expect(screen.queryByText('Herdr 状态异常')).not.toBeInTheDocument()
  })

  it('query error → danger degraded，不得 success → RuntimeMini 隐藏', async () => {
    const { container } = renderProjects()
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(container.querySelector(RUNTIME_MINI)).toBeNull()
    expect(screen.queryByText('Herdr 状态异常')).not.toBeInTheDocument()
  })
})
