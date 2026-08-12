import { apiGet, ApiError, ProtocolError } from '../api/client'
import { stateKindFromError } from '../api/errorState'

function mockResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

describe('api client（G3 错误模型）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('错误 envelope → ApiError 字段完整映射', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        mockResponse(409, {
          error: {
            code: 'conflict',
            message: '版本冲突',
            retryable: false,
            request_id: 'req-abc',
            details: { rev: 3 },
          },
        }),
      ),
    )
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('conflict')
    expect(err.message).toBe('版本冲突')
    expect(err.retryable).toBe(false)
    expect(err.requestId).toBe('req-abc')
    expect(err.status).toBe(409)
    expect(err.details).toEqual({ rev: 3 })
    expect(stateKindFromError(err)).toBe('conflict')
  })

  it('网络错误 → disconnected 映射', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed')
      }),
    )
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('disconnected')
    expect(err.retryable).toBe(true)
    expect(stateKindFromError(err)).toBe('disconnected')
  })

  it('transport_lost code → disconnected', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        mockResponse(502, {
          error: { code: 'transport_lost', message: '连接中断', retryable: true, request_id: 'req-1' },
        }),
      ),
    )
    const err = await apiGet('/api/x').catch((e) => e)
    expect(stateKindFromError(err)).toBe('disconnected')
  })

  it('403 → forbidden，stale code → stale', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(403, null)))
    expect(stateKindFromError(await apiGet('/api/x').catch((e) => e))).toBe('forbidden')

    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        mockResponse(200, { error: { code: 'data_stale', message: '缓存过期', retryable: true } }),
      ),
    )
    expect(stateKindFromError(await apiGet('/api/x').catch((e) => e))).toBe('stale')
  })

  it('G3 strict：裸对象（无 data/meta 键）→ ProtocolError，不透传', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, { hello: 'world' })))
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ProtocolError)
    expect(err.code).toBe('protocol_error')
  })

  it('G3 strict：数组 / null / 非 JSON 都 → ProtocolError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, [1, 2])))
    expect((await apiGet('/api/x').catch((e) => e)).code).toBe('protocol_error')

    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, null)))
    expect((await apiGet('/api/x').catch((e) => e)).code).toBe('protocol_error')

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError('Unexpected token')
        },
      }) as unknown as Response),
    )
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ProtocolError)
    expect(err.message).toContain('JSON')
  })

  it('data/meta envelope 透出 meta（partial / sources 保留）', async () => {
    const meta = {
      request_id: 'req-9',
      generated_at: '2026-08-12T00:00:00Z',
      partial: true,
      sources: [{ name: 'herdr', status: 'degraded', observed_at: null, reason: '超时' }],
    }
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, { data: { a: 1 }, meta })))
    const res = await apiGet<{ a: number }>('/api/x')
    expect(res.data).toEqual({ a: 1 })
    expect(res.meta?.partial).toBe(true)
    expect(res.meta?.sources?.[0].status).toBe('degraded')
  })

  it('空数组字段原样返回，不伪装完整结果', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockResponse(200, { data: { items: [] }, meta: { request_id: 'r-1' } })),
    )
    const res = await apiGet<{ items: unknown[]; projects?: unknown[] }>('/api/x')
    expect(res.data.items).toEqual([])
    expect(res.data.projects).toBeUndefined()
  })

  it('envelope 只有 data 无 meta → ProtocolError（缺 meta）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, { data: { a: 1 } })))
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ProtocolError)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('protocol_error')
    expect(err.retryable).toBe(false)
    expect(err.message).toContain('meta')
  })

  it('envelope 只有 meta 无 data → ProtocolError（缺 data）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => mockResponse(200, { meta: { request_id: 'r-1' } })))
    const err = await apiGet('/api/x').catch((e) => e)
    expect(err).toBeInstanceOf(ProtocolError)
    expect(err.message).toContain('data')
    expect(stateKindFromError(err)).toBe('error')
  })

  it('完整 envelope 正常返回 data + meta', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        mockResponse(200, { data: { ok: true }, meta: { request_id: 'r-9', sources: [] } }),
      ),
    )
    const res = await apiGet<{ ok: boolean }>('/api/x')
    expect(res.data).toEqual({ ok: true })
    expect(res.meta?.request_id).toBe('r-9')
  })
})
