import { screen, waitFor, within } from '@testing-library/react'
import {
  defaultFetchMap,
  metaOk,
  REG_P1,
  registryProjectsEmptyPayload,
} from '../fixtures/api'

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
import { renderApp, stubFetch } from './helpers'

describe('首用 Overview 与 Rail', () => {
  it('Registry 零项目时显示开始入口并隐藏提问与回复', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    renderApp('/overview')

    expect(await screen.findByText('还没有项目')).toBeInTheDocument()
    expect(screen.getByText('选择代码目录，添加第一个项目。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '选择代码目录' })).toBeInTheDocument()
    expect(screen.queryByText(/待决定事项会聚合到这里/)).not.toBeInTheDocument()
    expect(screen.queryByText('添加第一个项目', { selector: '.page-title' })).not.toBeInTheDocument()

    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(within(rail).getByTitle('项目')).toBeInTheDocument()
    expect(within(rail).queryByTitle('提问与回复')).not.toBeInTheDocument()
    expect(within(rail).queryByTitle('开始使用')).not.toBeInTheDocument()
    expect(within(rail).queryByTitle('设置')).not.toBeInTheDocument()
  })

  it('已有项目时保留 attention 标题和完整全局导航 → 改为项目列表，隐藏 Attention/Inbox', async () => {
    stubFetch(defaultFetchMap())
    renderApp('/overview')

    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByText('需要你处理', { selector: '.page-title' })).not.toBeInTheDocument()
    expect(screen.queryByText('跨项目聚合的待决定、待回复与提醒')).not.toBeInTheDocument()

    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => expect(within(rail).getByTitle('项目')).toBeInTheDocument())
    expect(within(rail).queryByTitle('需要你处理')).not.toBeInTheDocument()
    expect(within(rail).queryByTitle('提问与回复')).not.toBeInTheDocument()
    expect(within(rail).queryByTitle('开始使用')).not.toBeInTheDocument()
  })

  it('工作空间侧栏使用用户语言标题', async () => {
    stubFetch({ ...defaultFetchMap(), [WORK_ITEMS]: emptyWorkItems })
    renderApp('/projects/p1/workspaces/w1')

    const rail = screen.getByRole('navigation', { name: '主导航' })
    expect(await within(rail).findByText('当前工作空间')).toBeInTheDocument()
    expect(within(rail).queryByText('当前 Workspace')).not.toBeInTheDocument()
  })
})
