// 团队区：登录后显示已加入的 topic，绑定本机 Session

import { useState } from 'react'
import type { TeamTopic, TeamBinding } from './model'

interface TeamZoneProps {
  loggedIn: boolean
  username: string | null
  topics: TeamTopic[]
  bindings: TeamBinding[]
  localSessions: Array<{ name: string; label: string }>
  activeTopic: string | null
  onLogin: (username: string, password: string) => Promise<void>
  onLogout: () => Promise<void>
  onBindSession: (projectSlug: string, sessionName: string) => Promise<void>
  onSelectTopic: (projectSlug: string) => void
}

export function TeamZone({
  loggedIn,
  username,
  topics,
  bindings,
  localSessions,
  activeTopic,
  onLogin,
  onLogout,
  onBindSession,
  onSelectTopic,
}: TeamZoneProps) {
  const [showLogin, setShowLogin] = useState(false)
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [loginLoading, setLoginLoading] = useState(false)
  const [loginError, setLoginError] = useState<string | null>(null)

  const [bindingTopic, setBindingTopic] = useState<string | null>(null)

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!loginUsername.trim() || !loginPassword) return

    setLoginLoading(true)
    setLoginError(null)
    try {
      await onLogin(loginUsername.trim(), loginPassword)
      setShowLogin(false)
      setLoginUsername('')
      setLoginPassword('')
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoginLoading(false)
    }
  }

  const handleBind = async (projectSlug: string, sessionName: string) => {
    try {
      await onBindSession(projectSlug, sessionName)
      setBindingTopic(null)
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    }
  }

  if (!loggedIn) {
    if (!showLogin) {
      return (
        <div className="gc-team-zone">
          <div className="gc-side-group">团队</div>
          <button
            type="button"
            className="gc-team-login-btn"
            onClick={() => setShowLogin(true)}
          >
            登录团队账号
          </button>
          <div className="gc-team-reply-note">
            Human 账号：未登录 · Agent 自动回复：已暂停
          </div>
        </div>
      )
    }

    return (
      <div className="gc-team-zone">
        <div className="gc-side-group">团队登录</div>
        <form className="gc-team-login-form" onSubmit={handleLogin}>
          <input
            type="text"
            placeholder="用户名"
            value={loginUsername}
            onChange={(e) => setLoginUsername(e.target.value)}
            disabled={loginLoading}
            autoComplete="username"
          />
          <input
            type="password"
            placeholder="密码"
            value={loginPassword}
            onChange={(e) => setLoginPassword(e.target.value)}
            disabled={loginLoading}
            autoComplete="current-password"
          />
          {loginError && <div className="gc-team-error">{loginError}</div>}
          <div className="gc-team-actions">
            <button type="submit" disabled={loginLoading}>
              {loginLoading ? '登录中…' : '登录'}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowLogin(false)
                setLoginError(null)
              }}
              disabled={loginLoading}
            >
              取消
            </button>
          </div>
        </form>
      </div>
    )
  }

  return (
    <div className="gc-team-zone">
      <div className="gc-side-group">
        团队 ({username})
        <button
          type="button"
          className="gc-team-logout"
          onClick={() => void onLogout()}
          title="退出登录"
        >
          ⎋
        </button>
      </div>

      {topics.length === 0 && (
        <div className="gc-team-empty">还没有加入任何 topic</div>
      )}

      {topics.map((topic) => {
        const binding = bindings.find((b) => b.project_slug === topic.slug)
        const isBound = !!binding
        const bindingIsLive = !!binding && localSessions.some((sess) => sess.name === binding.session)
        const isBinding = bindingTopic === topic.slug

        return (
          <div key={topic.slug} className="gc-team-topic">
            <button
              type="button"
              className={`gc-team-topic-btn${
                activeTopic === topic.slug ? ' is-active' : ''
              }${!isBound ? ' is-unbound' : ''}`}
              onClick={() => onSelectTopic(topic.slug)}
              disabled={!isBound}
              title={
                isBound
                  ? `打开 ${topic.name}（绑定到 ${binding.session}${bindingIsLive ? '' : '，已停止'}）`
                  : `${topic.name}（需要先绑定本机 Session）`
              }
            >
              <span className="gc-team-topic-name">{topic.name}</span>
              {isBound && (
                <span className="gc-team-topic-session">
                  {binding.session}{bindingIsLive ? '' : '（已停止）'}
                </span>
              )}
            </button>

            {!isBinding && (
              <button
                type="button"
                className="gc-team-bind-trigger"
                onClick={() => setBindingTopic(topic.slug)}
                title={isBound ? '更换本机 Session' : '绑定本机 Session'}
              >
                {isBound ? '改绑' : '绑定'}
              </button>
            )}

            {isBinding && (
              <div className="gc-team-bind-picker">
                <div className="gc-team-bind-label">选择本机 Session：</div>
                {localSessions.map((sess) => (
                  <button
                    key={sess.name}
                    type="button"
                    className="gc-team-bind-session"
                    onClick={() => handleBind(topic.slug, sess.name)}
                  >
                    {sess.label}
                  </button>
                ))}
                <button
                  type="button"
                  className="gc-team-bind-cancel"
                  onClick={() => setBindingTopic(null)}
                >
                  取消
                </button>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
