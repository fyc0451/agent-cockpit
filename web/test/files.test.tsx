import { screen, waitFor } from '@testing-library/react'
import { renderApp, stubDefaultFetch } from './helpers'

describe('Files 页（files.read 关闭，item 1）', () => {
  it('深链冷启动：forbidden + 原因 + docs 入口，且零 /api/files 请求', async () => {
    const fetchSpy = stubDefaultFetch()
    const { container } = renderApp('/projects/p1/workspaces/w1/files')

    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Workspace 文件 facade API 未接通/),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看路线图' })).toHaveAttribute(
      'href',
      '#/settings?view=doctor',
    )
    expect(screen.getByRole('link', { name: '打开终端' })).toHaveAttribute(
      'href',
      '/projects/p1/workspaces/w1/terminal',
    )

    // 等 project/herdr 等其它请求全部落地，再断言没有任何 files 相关调用
    await waitFor(() => {
      expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    })
    expect(fetchSpy).toHaveBeenCalled()
    const filesCalls = fetchSpy.mock.calls.filter((c) => String(c[0]).startsWith('/api/files'))
    expect(filesCalls).toEqual([])
  })
})
