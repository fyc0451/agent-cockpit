import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubFetch, type MockResponseSpec } from './helpers'

function overviewSpec(spec: MockResponseSpec) {
  return stubFetch((url) => {
    if (url.startsWith('/api/overview')) return spec
    if (url.startsWith('/api/attention')) return { body: { data: { items: [] }, meta: {} } }
    if (url.startsWith('/api/herdr/status')) return { body: { data: { name: 'Herdr' }, meta: {} } }
    return undefined
  })
}

describe('QueryErrorState 按 retryable 给 action（item 5）', () => {
  it('retryable 的 disconnected 显示重试按钮，点击触发 refetch', async () => {
    const fetchSpy = vi.fn(async () => {
      throw new TypeError('fetch failed')
    })
    vi.stubGlobal('fetch', fetchSpy)
    const { container } = renderApp('/projects')

    // hook 对 retryable 错误做 2 次退避重试（~3s），等查询落定
    const retryBtn = await screen.findByRole('button', { name: '重试' }, { timeout: 8000 })
    expect(container.querySelector('[data-state="disconnected"]')).toBeInTheDocument()

    const before = fetchSpy.mock.calls.length
    await userEvent.setup().click(retryBtn)
    await waitFor(() => {
      expect(fetchSpy.mock.calls.length).toBeGreaterThan(before)
    })
  })

  it('forbidden(403) 无重试按钮，显示 code/message/request_id + docs 入口', async () => {
    overviewSpec({
      status: 403,
      body: {
        error: { code: 'forbidden', message: '没有访问权限', retryable: false, request_id: 'req-f1' },
      },
    })
    const { container } = renderApp('/projects')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    expect(screen.getByText('没有访问权限')).toBeInTheDocument()
    expect(screen.getByText(/错误码：forbidden/)).toBeInTheDocument()
    expect(screen.getByText(/request_id: req-f1/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看路线图' })).toBeInTheDocument()
  })

  it('conflict(409) 无重试按钮', async () => {
    overviewSpec({
      status: 409,
      body: { error: { code: 'conflict', message: '版本冲突', retryable: false, request_id: 'req-c1' } },
    })
    const { container } = renderApp('/projects')
    await waitFor(() => {
      expect(container.querySelector('[data-state="conflict"]')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  it('protocol_error（envelope 缺 meta）无重试按钮', async () => {
    overviewSpec({ body: { data: { projects: [] } } })
    const { container } = renderApp('/projects')
    await waitFor(() => {
      expect(container.querySelector('[data-state="error"]')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    expect(screen.getByText(/缺少 meta 键/)).toBeInTheDocument()
    expect(screen.getByText(/错误码：protocol_error/)).toBeInTheDocument()
  })

  it('server_error(500, retryable) 显示重试按钮', async () => {
    overviewSpec({ status: 500, body: null })
    renderApp('/projects')
    // hook 对 retryable 错误做 2 次退避重试（~3s），等查询落定
    expect(
      await screen.findByRole('button', { name: '重试' }, { timeout: 8000 }),
    ).toBeInTheDocument()
  })
})
