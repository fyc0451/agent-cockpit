import { screen, waitFor, within } from '@testing-library/react'
import { projectP1 } from '../fixtures/api'
import { renderApp, stubDefaultFetch, stubFetch } from './helpers'

// Bare legacy shapes matching real server.py (WEB-004 hooks use legacyGet)
const legacyOverview = { projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: { available: true, reason: null } }
const legacyAttention = { sessions: [], items: [{ id: 'a1', kind: 'review', title: 'ReviewPacket 待决定', summary: 'run r-9 变更需要人工决定', status: 'needs-action', project: 'p1' }, { id: 'a2', kind: 'note', title: '恢复提醒', summary: 'w2 可恢复会话', status: 'info', project: 'p1' }], count: 2, mail_unread: 0, capabilities: {} }
const legacyHerdr = { available: true, binary: '/usr/local/bin/herdr' }
const legacySettings = { language: 'zh', known_agents: ['codex', 'kimi', 'claude'], languages: ['zh', 'en'] }
const legacyOverrides = { '/api/overview': legacyOverview, '/api/attention': legacyAttention, '/api/herdr/status': legacyHerdr, '/api/settings': legacySettings }

describe('深链恢复（G1）', () => {
  it('从 #/projects/p1/workspaces/w1/files 冷启动恢复 selection Context', async () => {
    stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1/files')

    // 页面本体：files.read 关闭 → forbidden（零 files 请求的专项断言见 files.test.tsx）
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })

    // rail 已恢复 project + workspace selection
    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => {
      expect(within(rail).getByText('当前项目')).toBeInTheDocument()
      expect(within(rail).getByText('当前 Workspace')).toBeInTheDocument()
      expect(within(rail).getByText('Project One')).toBeInTheDocument()
      expect(within(rail).getAllByText('本机工作区').length).toBeGreaterThan(0)
    })

    // 顶栏 switcher 同样恢复
    expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    expect(screen.getByTitle('切换 Workspace')).toHaveTextContent('本机工作区')
  })

  it('无效 project slug（404 envelope）显示 typed error 态，不得用 empty', async () => {
    stubFetch({
      '/api/attention': { items: [], sessions: [], count: 0, mail_unread: 0, capabilities: {} },
      '/api/herdr/status': { available: true, binary: '/usr/local/bin/herdr' },
      '/api/overview': legacyOverview,
      '/api/settings': legacySettings,
      // /api/projects/nope 未配置 → 404 envelope
    })
    const { container } = renderApp('/projects/nope/workbench')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.getByText('项目不存在')).toBeInTheDocument()
    expect(screen.getByText(/no mock/i)).toBeInTheDocument()
  })

  it('workspace 不在 project 列表中（mismatch）显示 typed error 态，不得用 empty', async () => {
    stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/nope')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-state="empty"]')).not.toBeInTheDocument()
    expect(screen.getByText('Workspace 不存在或不属于当前项目')).toBeInTheDocument()
  })

  it('未知路由重定向 #/overview', async () => {
    stubDefaultFetch(legacyOverrides)
    renderApp('/no/such/route')
    expect(await screen.findByText('需要你处理', { selector: '.page-title' })).toBeInTheDocument()
  })

  it('#/settings?view=doctor 直达环境自检 tab', async () => {
    stubDefaultFetch()
    renderApp('/settings?view=doctor')

    const doctorTab = await screen.findByRole('tab', { name: '环境自检' })
    expect(doctorTab).toHaveAttribute('aria-selected', 'true')
    // harness tab 未选中
    expect(screen.getByRole('tab', { name: 'Harness / Runtime 与节点' })).toHaveAttribute(
      'aria-selected',
      'false',
    )
    // doctor 数据源真实渲染（裸 legacy env-check：agents + herdr 失败原因）
    expect(await screen.findByText('codex')).toBeInTheDocument()
    expect(screen.getByText('Herdr 未运行')).toBeInTheDocument()
  })

  it('#/inbox?view=needs-action 从 query 恢复筛选', async () => {
    stubDefaultFetch(legacyOverrides)
    renderApp('/inbox?view=needs-action')
    const tab = await screen.findByRole('tab', { name: '需要你处理' })
    expect(tab).toHaveAttribute('aria-selected', 'true')
    // needs-action 项保留，info 项被过滤
    expect(await screen.findByText('ReviewPacket 待决定')).toBeInTheDocument()
    expect(screen.queryByText('恢复提醒')).not.toBeInTheDocument()
  })

  it('workbench 深链渲染区块数据', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workbench')
    expect(await screen.findByText('修复登录回归')).toBeInTheDocument()
    expect(screen.getByText('kimi')).toBeInTheDocument()
  })

  it('后端返回 404 envelope 的 slug 显示 error 态', async () => {
    // /api/projects/p2 不在默认映射中 → 默认 404 envelope
    stubDefaultFetch()
    const { container } = renderApp('/projects/p2/workbench')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(projectP1.slug).toBe('p1') // fixture 引用防 tree-shake 误报
  })
})
