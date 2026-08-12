import type { ResponseMeta } from './types'

export interface ApiErrorInit {
  code: string
  message: string
  retryable: boolean
  requestId?: string | null
  status?: number
  details?: unknown
}

/** G3 冻结错误模型：{ error: { code, message, retryable, request_id, details } } */
export class ApiError extends Error {
  code: string
  retryable: boolean
  requestId?: string | null
  status?: number
  details?: unknown

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.retryable = init.retryable
    this.requestId = init.requestId ?? null
    this.status = init.status
    this.details = init.details
  }
}

/** G3 协议错误：envelope 形态不完整（data/meta 缺任一键），retryable=false */
export class ProtocolError extends ApiError {
  constructor(message: string, init?: Partial<Omit<ApiErrorInit, 'code' | 'message' | 'retryable'>>) {
    super({ code: 'protocol_error', retryable: false, ...init, message })
    this.name = 'ProtocolError'
  }
}

export interface ApiResult<T> {
  data: T
  meta: ResponseMeta | null
}

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    retryable?: boolean
    request_id?: string
    details?: unknown
  } | null
}

interface DataEnvelope<T> {
  data?: T
  meta?: ResponseMeta
}

function codeForStatus(status: number): string {
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status >= 500) return 'server_error'
  return 'http_error'
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

/**
 * fetch wrapper：解析 envelope，错误映射 ApiError，网络失败映射 disconnected。
 * meta.partial=true 或 sources 含非 available 时 meta 原样透出，由页面渲染 degraded 态。
 */
export async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  let res: Response
  try {
    res = await fetch(path, { headers: { Accept: 'application/json' } })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }

  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    body = null
  }

  if (isObject(body) && 'error' in body && (body as ErrorEnvelope).error) {
    const e = (body as ErrorEnvelope).error!
    throw new ApiError({
      code: e.code ?? codeForStatus(res.status),
      message: e.message ?? `请求失败（HTTP ${res.status}）`,
      retryable: e.retryable ?? res.status >= 500,
      requestId: e.request_id ?? null,
      status: res.status,
      details: e.details,
    })
  }

  if (!res.ok) {
    throw new ApiError({
      code: codeForStatus(res.status),
      message: `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }

  if (isObject(body) && ('data' in body || 'meta' in body)) {
    // envelope 严格校验：data/meta 出现任一键，两者就都必须存在
    if (!('data' in body)) {
      throw new ProtocolError('响应 envelope 缺少 data 键', { status: res.status })
    }
    if (!('meta' in body)) {
      throw new ProtocolError('响应 envelope 缺少 meta 键', { status: res.status })
    }
    const env = body as DataEnvelope<T>
    return { data: env.data as T, meta: env.meta ?? null }
  }
  // legacy 容忍路径：完全不含 data/meta 键的裸响应（非 envelope），整个 body 按 data 透传
  return { data: body as T, meta: null }
}

export const api = { get: apiGet }
