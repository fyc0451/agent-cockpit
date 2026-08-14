import { FormEvent, useEffect, useState, type ReactNode } from 'react'
import { Button } from '../components/Button'
import { StatusState } from '../components/StatusState'

interface AuthStatus {
  required: boolean
  authenticated: boolean
  local_only: boolean
}

type Phase = 'checking' | 'login' | 'ready' | 'error'

function isAuthStatus(value: unknown): value is AuthStatus {
  if (typeof value !== 'object' || value === null) return false
  const status = value as Record<string, unknown>
  return (
    typeof status.required === 'boolean' &&
    typeof status.authenticated === 'boolean' &&
    typeof status.local_only === 'boolean'
  )
}

async function fetchAuthStatus(signal?: AbortSignal): Promise<AuthStatus> {
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

function messageFromLoginFailure(status: number): string {
  if (status === 401) return '访问令牌无效'
  if (status === 403) return '当前地址不允许登录'
  return `登录失败（HTTP ${status}）`
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>('checking')
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setPhase('checking')
    setError(null)
    void fetchAuthStatus(controller.signal)
      .then((status) => setPhase(!status.required || status.authenticated ? 'ready' : 'login'))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : '无法连接认证服务')
        setPhase('error')
      })
    return () => controller.abort()
  }, [retry])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!token || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      })
      if (!response.ok) {
        setError(messageFromLoginFailure(response.status))
        return
      }
      const status = await fetchAuthStatus()
      if (!status.authenticated) {
        setError('登录状态未生效，请重试')
        return
      }
      setToken('')
      setPhase('ready')
    } catch {
      setError('无法连接认证服务')
    } finally {
      setSubmitting(false)
    }
  }

  if (phase === 'ready') return children

  if (phase === 'checking') {
    return (
      <main className="auth-screen">
        <StatusState kind="loading" title="正在连接 Cockpit…" />
      </main>
    )
  }

  if (phase === 'error') {
    return (
      <main className="auth-screen">
        <StatusState
          kind="error"
          title="无法连接 Cockpit"
          description={error ?? '认证服务不可用'}
          children={
            <div className="state-actions">
              <Button variant="primary" onClick={() => setRetry((value) => value + 1)}>
                重试
              </Button>
            </div>
          }
        />
      </main>
    )
  }

  return (
    <main className="auth-screen">
      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="auth-brand" aria-hidden="true">AC</div>
        <div className="auth-heading">
          <h1 id="auth-title">Agent Cockpit</h1>
          <p>此实例需要访问令牌</p>
        </div>
        <form onSubmit={submit}>
          <label className="auth-label" htmlFor="cockpit-token">访问令牌</label>
          <input
            id="cockpit-token"
            className="input auth-input"
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
          />
          {error ? <p className="auth-error" role="alert">{error}</p> : null}
          <Button className="auth-submit" variant="primary" type="submit" disabled={!token || submitting}>
            {submitting ? '登录中…' : '登录'}
          </Button>
        </form>
      </section>
    </main>
  )
}
