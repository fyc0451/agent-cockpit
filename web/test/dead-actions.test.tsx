import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { registryProjectsEmptyPayload, registryProjectsPayload } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

describe('Settings 去掉死保存、JSON 进高级详情', () => {
  it('Harness 页无「保存设置」，只读说明在首屏，JSON 默认折叠 → /settings 回到项目列表', async () => {
    stubDefaultFetch()
    renderApp('/settings')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('当前为只读：修改将在后续版本开放')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存设置' })).toBeNull()
    expect(screen.queryByText('高级详情')).not.toBeInTheDocument()
  })
})

describe('Unavailable 不再跳 Doctor 假路线图', () => {
  it('项目未开放页：返回项目列表，无「查看路线图」', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/memory')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('该能力暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '查看路线图' })).toBeNull()
    expect(screen.queryByRole('link', { name: '返回项目概览' })).toBeNull()
  })

  it('工作空间未开放页：回到项目列表，无「查看路线图」', async () => {
    stubDefaultFetch()
    renderApp('/projects/p1/workspaces/w1/git')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('该能力暂不可用')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '查看路线图' })).toBeNull()
    expect(screen.queryByRole('link', { name: '返回工作空间' })).toBeNull()
  })
})

describe('Inbox 空态入口与可解析才可点', () => {
  it('零项目空态：选择代码目录打开向导（/inbox → 项目列表）', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    renderApp('/inbox')
    const cta = await screen.findByRole('button', { name: '选择代码目录' })
    expect(screen.queryByRole('link', { name: '选择代码目录' })).toBeNull()
    await userEvent.click(cta)
    expect(await screen.findByRole('dialog', { name: '添加项目' })).toBeInTheDocument()
  })

  it('已有项目空态：查看项目进列表（/inbox → 项目列表）', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsPayload,
    })
    renderApp('/inbox')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(await screen.findByText('Alpha 项目')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '查看项目' })).toBeNull()
  })

  it('project 等于唯一 slug 或唯一显示名才可点；无法解析保持纯文本 → Inbox 隐藏', async () => {
    stubDefaultFetch({
      '/api/project-registry/projects': registryProjectsPayload,
    })
    renderApp('/inbox')
    expect(await screen.findByText('Project One')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '按 slug 打开' })).toBeNull()
    expect(screen.queryByText('未知项目')).not.toBeInTheDocument()
  })

  it('同名显示名不唯一时不猜 slug → Inbox 隐藏，不伪造可点事项', async () => {
    const dup = structuredClone(registryProjectsPayload)
    dup.data.items[3].project.display_name = 'Project One'
    stubDefaultFetch({
      '/api/project-registry/projects': dup,
    })
    renderApp('/inbox')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '重名事项' })).toBeNull()
    expect(screen.queryByText('重名事项')).not.toBeInTheDocument()
  })
})
