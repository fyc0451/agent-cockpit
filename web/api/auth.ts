import { ApiError } from './client'
import { reportUnauthorized } from './authEvents'

export interface AuthStatus {
  required: boolean
  authenticated: boolean
  local_only: boolean
}

export { reportUnauthorized, subscribeUnauthorized, noteAuthFailure } from './authEvents'

export function isAuthStatus(value: unknown): value is AuthStatus {
  if (typeof value !== 'object' || value === null) return false
  const status = value as Record<string, unknown>
  return (
    typeof status.required === 'boolean' &&
    typeof status.authenticated === 'boolean' &&
    typeof status.local_only === 'boolean'
  )
}

export function isUnauthenticatedError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status === 401 || error.code === 'unauthenticated' || error.message === '未认证'
  }
  return false
}

export async function fetchAuthStatus(signal?: AbortSignal): Promise<AuthStatus> {
  const response = await fetch('/api/auth/status', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal,
  })
  if (!response.ok) throw new Error(`认证状态请求失败（HTTP ${response.status}）`)
  const body: unknown = await response.json()
  if (!isAuthStatus(body)) throw new Error('认证状态响应无效')
  return body
}

/** 写操作前探活。未登录则拉回登录页，不发出业务请求。 */
export async function requireAuthenticated(): Promise<void> {
  const status = await fetchAuthStatus()
  if (status.required && !status.authenticated) {
    reportUnauthorized()
    throw new ApiError({
      code: 'unauthenticated',
      message: '未认证',
      retryable: false,
      status: 401,
    })
  }
}
