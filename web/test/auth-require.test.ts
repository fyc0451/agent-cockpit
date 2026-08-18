import { ApiError } from '../api/client'
import { requireAuthenticated } from '../api/auth'

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

describe('requireAuthenticated', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('token 未启用时放行', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(200, {
      required: false,
      authenticated: true,
      local_only: true,
    })))
    await expect(requireAuthenticated()).resolves.toBeUndefined()
  })

  it('已登录时放行', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(200, {
      required: true,
      authenticated: true,
      local_only: false,
    })))
    await expect(requireAuthenticated()).resolves.toBeUndefined()
  })

  it('需要登录但未认证时抛错，不让业务请求发出去', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(200, {
      required: true,
      authenticated: false,
      local_only: false,
    })))
    await expect(requireAuthenticated()).rejects.toMatchObject({
      code: 'unauthenticated',
      message: '未认证',
      status: 401,
    })
    await expect(requireAuthenticated()).rejects.toBeInstanceOf(ApiError)
  })
})
