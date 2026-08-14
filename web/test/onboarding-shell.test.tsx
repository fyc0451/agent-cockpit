import { screen, waitFor, within } from '@testing-library/react'
import {
  defaultFetchMap,
  registryProjectsEmptyPayload,
} from '../fixtures/api'
import { renderApp, stubFetch } from './helpers'

describe('首用 Overview 与 Rail', () => {
  it('Registry 零项目时显示开始入口并隐藏提问与回复', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/project-registry/projects': registryProjectsEmptyPayload,
    })
    renderApp('/overview')

    expect(
      await screen.findByText('添加第一个项目', { selector: '.page-title' }),
    ).toBeInTheDocument()
    expect(screen.getByText('选择一个代码目录即可开始', { selector: '.page-sub' })).toBeInTheDocument()
    expect(screen.queryByText(/待决定事项会聚合到这里/)).not.toBeInTheDocument()

    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => expect(within(rail).getByTitle('开始使用')).toBeInTheDocument())
    expect(within(rail).queryByTitle('提问与回复')).not.toBeInTheDocument()
    expect(within(rail).getByTitle('项目')).toBeInTheDocument()
    expect(within(rail).getByTitle('设置')).toBeInTheDocument()
  })

  it('已有项目时保留 attention 标题和完整全局导航', async () => {
    stubFetch(defaultFetchMap())
    renderApp('/overview')

    expect(
      await screen.findByText('需要你处理', { selector: '.page-title' }),
    ).toBeInTheDocument()
    expect(screen.getByText('跨项目聚合的待决定、待回复与提醒')).toBeInTheDocument()

    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => expect(within(rail).getByTitle('需要你处理')).toBeInTheDocument())
    expect(within(rail).getByTitle('提问与回复')).toBeInTheDocument()
    expect(within(rail).queryByTitle('开始使用')).not.toBeInTheDocument()
  })
})
