import { screen, waitFor, within } from '@testing-library/react'
import { defaultFetchMap, metaOk, projectP1, REG_P1, REG_P2 } from '../fixtures/api'
import { renderApp, stubDefaultFetch, stubFetch } from './helpers'

const P1_WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const P2_WORK_ITEMS = `/api/projects/${REG_P2}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }

const legacyOverview = { projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: { available: true, reason: null } }
const legacyAttention = { sessions: [], items: [{ id: 'a1', kind: 'review', title: 'ReviewPacket 待决定', summary: 'run r-9 变更需要人工决定', status: 'needs-action', project: 'p1' }, { id: 'a2', kind: 'note', title: '恢复提醒', summary: 'w2 可恢复会话', status: 'info', project: 'p1' }], count: 2, mail_unread: 0, capabilities: {} }
const legacyHerdr = { available: true, binary: '/usr/local/bin/herdr' }
const legacySettings = { language: 'zh', known_agents: ['codex', 'kimi', 'claude'], languages: ['zh', 'en'] }
const legacyOverrides = {
  '/api/overview': legacyOverview,
  '/api/attention': legacyAttention,
  '/api/herdr/status': legacyHerdr,
  '/api/settings': legacySettings,
  [P1_WORK_ITEMS]: emptyWorkItems,
}

describe('深链恢复（G1）', () => {
  it('从 #/projects/p1/workspaces/w1/files 冷启动恢复 selection Context', async () => {
    stubDefaultFetch({ [P1_WORK_ITEMS]: emptyWorkItems })
    const { container } = renderApp('/projects/p1/workspaces/w1/files')

    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(screen.getByText('文件浏览暂不可用')).toBeInTheDocument()

    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => {
      expect(within(rail).getByText('当前项目')).toBeInTheDocument()
      expect(within(rail).getByText('当前工作空间')).toBeInTheDocument()
      expect(within(rail).getByText('Project One')).toBeInTheDocument()
      expect(within(rail).getAllByText('本机工作区').length).toBeGreaterThan(0)
    })

    expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('本机工作区')
  })

  it('无效 project slug：Registry 列表成功后按 slug 不匹配报「项目不存在」', async () => {
    stubFetch({
      ...defaultFetchMap(),
      [P1_WORK_ITEMS]: emptyWorkItems,
    })
    renderApp('/projects/nope/workbench')
    expect(await screen.findByText('项目不存在')).toBeInTheDocument()
    expect(screen.getByText(/未找到项目「nope」/)).toBeInTheDocument()
    expect(screen.queryByText('还没有项目')).not.toBeInTheDocument()
    expect(screen.queryByText(/no mock/i)).not.toBeInTheDocument()
    expect(projectP1.slug).toBe('p1')
  })

  it('workspace 不在 project 列表中（mismatch）显示 typed error 态，不得用 empty', async () => {
    stubDefaultFetch({ [P1_WORK_ITEMS]: emptyWorkItems })
    renderApp('/projects/p1/workspaces/nope')
    expect(await screen.findByText('工作空间不存在或不属于当前项目')).toBeInTheDocument()
    expect(screen.getByText(/没有 ID 为「nope」的工作空间/)).toBeInTheDocument()
    expect(screen.queryByText('还没有本机工作空间')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('今天想推进什么？')).not.toBeInTheDocument()
  })

  it('未知路由重定向 #/projects', async () => {
    stubDefaultFetch(legacyOverrides)
    renderApp('/no/such/route')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('需要你处理', { selector: '.page-title' })).not.toBeInTheDocument()
  })

  it('#/settings?view=doctor 不再直达环境自检，回到项目列表', async () => {
    stubDefaultFetch({ ...legacyOverrides })
    renderApp('/settings?view=doctor')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '环境自检' })).not.toBeInTheDocument()
    expect(screen.queryByText('Herdr 未运行')).not.toBeInTheDocument()
  })

  it('#/inbox?view=needs-action 不再恢复 Inbox 筛选，回到项目列表', async () => {
    stubDefaultFetch(legacyOverrides)
    renderApp('/inbox?view=needs-action')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '需要你处理' })).not.toBeInTheDocument()
    expect(screen.queryByText('ReviewPacket 待决定')).not.toBeInTheDocument()
  })

  it('workbench 深链进入本机 Workspace Focus，不渲染 assignments', async () => {
    stubDefaultFetch({ [P1_WORK_ITEMS]: emptyWorkItems })
    renderApp('/projects/p1/workbench')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.queryByText('修复登录回归')).not.toBeInTheDocument()
    expect(screen.queryByText('kimi')).not.toBeInTheDocument()
  })

  it('p2 合法深链：Registry 命中后进入 Focus，必须显式 stub p2 work-items', async () => {
    stubDefaultFetch({
      [P1_WORK_ITEMS]: emptyWorkItems,
      [P2_WORK_ITEMS]: emptyWorkItems,
    })
    renderApp('/projects/p2/workbench')
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'B 工作区' })).toBeInTheDocument()
    expect(screen.getByTitle('切换项目')).toHaveTextContent('Project Two')
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('B 工作区')
    expect(screen.queryByText('项目不存在')).not.toBeInTheDocument()
    expect(screen.queryByText(/no mock/i)).not.toBeInTheDocument()
  })
})
