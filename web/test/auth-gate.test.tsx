import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { reportUnauthorized } from '../api/authEvents'
import { AuthGate } from '../features/AuthGate'

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

describe('AuthGate', () => {
  it('token 未启用时直接进入应用', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(200, {
      required: false,
      authenticated: true,
      local_only: true,
    })))

    render(<AuthGate><div>应用已加载</div></AuthGate>)

    expect(await screen.findByText('应用已加载')).toBeInTheDocument()
    expect(screen.queryByLabelText('密码')).not.toBeInTheDocument()
  })

  it('需要 token 时登录成功后进入应用，且不持久化 token', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, {
        required: true,
        authenticated: false,
        local_only: false,
      }))
      .mockResolvedValueOnce(response(200, { ok: true, required: true }))
      .mockResolvedValueOnce(response(200, {
        required: true,
        authenticated: true,
        local_only: false,
      }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<AuthGate><div>应用已加载</div></AuthGate>)
    const input = await screen.findByLabelText('密码')
    expect(screen.getByText(/自己定的短密码/)).toBeInTheDocument()
    expect(screen.getByText(/\.config\/agent-cockpit\/cockpit\.token/)).toBeInTheDocument()
    expect(screen.getByText(/cockpit\.token/)).toBeInTheDocument()
    expect(screen.getByText(/不要发到聊天或项目文件里/)).toBeInTheDocument()
    await user.type(input, 'test-token-value')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByText('应用已加载')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/auth/login', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      body: JSON.stringify({ token: 'test-token-value' }),
    }))
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('错误 token 留在登录页并显示稳定错误', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, {
        required: true,
        authenticated: false,
        local_only: false,
      }))
      .mockResolvedValueOnce(response(401, { detail: '令牌错误' }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<AuthGate><div>应用已加载</div></AuthGate>)
    await user.type(await screen.findByLabelText('密码'), 'wrong-token')
    await user.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('密码不对')
    expect(screen.queryByText('应用已加载')).not.toBeInTheDocument()
  })

  it('认证状态请求失败后可以重试', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response(200, {
        required: false,
        authenticated: true,
        local_only: true,
      }))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<AuthGate><div>应用已加载</div></AuthGate>)
    expect(await screen.findByText('无法连接 Cockpit')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试' }))

    await waitFor(() => expect(screen.getByText('应用已加载')).toBeInTheDocument())
  })

  it('会话失效时浮层登录，底下应用和草稿还在', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(200, {
      required: true,
      authenticated: true,
      local_only: false,
    })))
    render(<AuthGate><div>应用已加载</div></AuthGate>)
    expect(await screen.findByText('应用已加载')).toBeInTheDocument()

    act(() => {
      reportUnauthorized()
    })
    expect(screen.getByText('应用已加载')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('登录已失效。输入还在下面，登录后不用重打。')
  })
})
