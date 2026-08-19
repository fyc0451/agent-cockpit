import { FormEvent, useEffect, useState, type ReactNode } from 'react'
import { fetchAuthStatus, subscribeUnauthorized } from '../api/auth'
import { Button } from '../components/Button'
import { StatusState } from '../components/StatusState'

type Phase = 'checking' | 'login' | 'ready' | 'error'

function messageFromLoginFailure(status: number): string {
  if (status === 401) return '密码不对'
  if (status === 403) return '当前地址不允许登录'
  return `登录失败（HTTP ${status}）`
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>('checking')
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [retry, setRetry] = useState(0)
  const [keepApp, setKeepApp] = useState(false)

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

  useEffect(() => subscribeUnauthorized(() => {
    setKeepApp(true)
    setPhase('login')
    setError('登录已失效。输入还在下面，登录后不用重打。')
    setToken('')
  }), [])

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
      setKeepApp(false)
      setPhase('ready')
    } catch {
      setError('无法连接认证服务')
    } finally {
      setSubmitting(false)
    }
  }

  const loginForm = (
    <section className="auth-panel" aria-labelledby="auth-title">
      <div className="auth-brand" aria-hidden="true">AC</div>
      <div className="auth-heading">
        <h1 id="auth-title">Agent Cockpit</h1>
        <p>输入本机登录密码</p>
        <p className="auth-hint">
          自己定的短密码，写在这台电脑的 ~/.config/agent-cockpit/cockpit.token（8790）。
          4–64 位字母数字即可，不用记一长串。不要发到聊天或项目文件里。
        </p>
      </div>
      <form onSubmit={submit}>
        <label className="auth-label" htmlFor="cockpit-token">密码</label>
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
  )

  if (phase === 'ready') return children

  if (keepApp) {
    return (
      <>
        {children}
        <div className="auth-overlay" role="dialog" aria-modal="true" aria-labelledby="auth-title">
          {loginForm}
        </div>
      </>
    )
  }

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
      {loginForm}
    </main>
  )
}
