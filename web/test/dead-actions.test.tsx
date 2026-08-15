import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { registryProjectsEmptyPayload, registryProjectsPayload } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

const emptyAttention = {
  sessions: [],
  items: [],
  count: 0,
  mail_unread: 0,
  capabilities: {},
}

function attentionWith(items: Array<Record<string, string | undefined>>) {
  return {
    sessions: [],
    items,
    count: items.length,
    mail_unread: 0,
    capabilities: {},
  }
}

describe('Settings 去掉死保存、JSON 进高级详情', () => {
  it('Harness 页无「保存设置」，只读说明在首屏，JSON 默认折叠', async () => {
    stubDefaultFetch()
    renderApp('/settings')
    expect(await screen.findByText('当前为只读：修改将在后续版本开放')).toBeInTheDocument()
    expect(screen.getByText('设置当前只能查看，不能在此保存修改。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存设置' })).toBeNull()
    const details = (await screen.findByText('高级详情')).closest('details')
    expect(details).not.toBeNull()
    expect(details).not.toHaveAttribute('open')
    expect(within(details!).getByText(/"language": "zh"/)).toBeInTheDocument()
    await userEvent.click(screen.getByText('高级详情'))
    expect(details).toHaveAttribute('open')
  })
})

describe('Unavailable 不再跳 Doctor 假路线图', () => {
  it('项目未开放页：返回项目概览，无「查看路线图」', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/memory')
    await waitFor(() => {
      expect(screen.getByText('该能力暂不可用')).toBeInTheDocument()
    })
    expect(screen.queryByRole('link', { name: '查看路线图' })).toBeNull()
    const back = screen.getByRole('link', { name: '返回项目概览' })
    expect(back).toHaveAttribute('href', '/projects/p1/workbench')
  })

  it('工作空间未开放页：返回工作空间概览，无「查看路线图」', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workspaces/w1/git')
    await waitFor(() => {
      expect(screen.getByText('该能力暂不可用')).toBeInTheDocument()
    })
    expect(screen.queryByRole('link', { name: '查看路线图' })).toBeNull()
    const back = screen.getByRole('link', { name: '返回工作空间' })
    expect(back).toHaveAttribute('href', '/projects/p1/workspaces/w1')
  })
})

describe('Inbox 空态入口与可解析才可点', () => {
  it('零项目空态：选择代码目录打开向导', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsEmptyPayload,
      '/api/attention': emptyAttention,
    })
    renderApp('/inbox')
    const cta = await screen.findByRole('link', { name: '选择代码目录' })
    expect(cta).toHaveAttribute('href', '/projects?wizard=1')
  })

  it('已有项目空态：查看项目进列表', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsPayload,
      '/api/attention': emptyAttention,
    })
    renderApp('/inbox')
    const cta = await screen.findByRole('link', { name: '查看项目' })
    expect(cta).toHaveAttribute('href', '/projects')
  })

  it('project 等于唯一 slug 或唯一显示名才可点；无法解析保持纯文本', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsPayload,
      '/api/attention': attentionWith([
        { id: 'a1', title: '按 slug 打开', project: 'p1', status: 'open' },
        { id: 'a2', title: '按显示名打开', project: 'Project One', status: 'open' },
        { id: 'a3', title: '未知项目', project: 'not-a-project', status: 'open' },
        { id: 'a4', title: '没有项目字段', status: 'open' },
      ]),
    })
    renderApp('/inbox')
    const bySlug = await screen.findByRole('link', { name: '按 slug 打开' })
    expect(bySlug).toHaveAttribute('href', '/projects/p1/workbench')
    expect(screen.getByRole('link', { name: '按显示名打开' })).toHaveAttribute(
      'href',
      '/projects/p1/workbench',
    )
    expect(screen.getByText('未知项目').closest('a')).toBeNull()
    expect(screen.getByText('没有项目字段').closest('a')).toBeNull()
    expect(screen.queryByRole('link', { name: '未知项目' })).toBeNull()
  })

  it('同名显示名不唯一时不猜 slug', async () => {
    const dup = structuredClone(registryProjectsPayload)
    dup.data.items[3].project.display_name = 'Project One'
    stubDefaultFetch({
      '/api/project-registry/projects': dup,
      '/api/attention': attentionWith([
        { id: 'd1', title: '重名事项', project: 'Project One', status: 'open' },
      ]),
    })
    renderApp('/inbox')
    await screen.findByText('重名事项')
    expect(screen.queryByRole('link', { name: '重名事项' })).toBeNull()
  })
})
