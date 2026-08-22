// 左栏（3080 形态）：工作区列表（= 用户添加的工作目录），工作区下挂会话（群聊）。
// 第一步「＋ 添加工作区」选目录；第二步在工作区组头「＋」创建会话。
// Cockpit 4.0：配置 Team Hub 后，工作区下方显示团队区。

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { routes } from '../../app/routes'
import { statusMeta, type SessionRow } from './model'
import type { TeamTopic, TeamBinding } from '../team/model'

export interface WorkspaceGroup {
  root: string // 工作区目录
  label: string // basename
  removable: boolean // 仅 custom 来源可移除
  rows: SessionRow[] // 该工作区下的会话
}

interface SessionSidebarProps {
  groups: WorkspaceGroup[]
  ungrouped: SessionRow[] // 不属于任何工作区的会话
  activeSession: string | null
  loading: boolean
  onSelect: (session: string) => void
  onAddWorkspace: () => void
  onRemoveWorkspace: (root: string) => void
  onNewSession: (root: string) => void
  // Cockpit 4.0: 团队区
  teamEnabled?: boolean
  teamLoggedIn?: boolean
  teamUsername?: string | null
  teamTopics?: TeamTopic[]
  teamBindings?: TeamBinding[]
  teamActiveTopic?: string | null
  onTeamLogin?: (username: string, password: string) => Promise<void>
  onTeamLogout?: () => Promise<void>
  onTeamBindSession?: (projectSlug: string, sessionName: string) => Promise<void>
  onTeamSelectTopic?: (projectSlug: string) => void
}

function SessionItem({
  row,
  active,
  onSelect,
}: {
  row: SessionRow
  active: boolean
  onSelect: () => void
}) {
  const meta = statusMeta(row.status)
  return (
    <button
      type="button"
      className={`gc-session${active ? ' is-active' : ''}`}
      onClick={onSelect}
      title={`${row.name} · ${meta.label}`}
    >
      <span className={`gc-dot ${meta.dot}`} aria-hidden />
      <span className="gc-session-name">{row.name}</span>
      <span className="gc-session-meta">{row.memberCount} 人</span>
    </button>
  )
}

export function SessionSidebar({
  groups,
  ungrouped,
  activeSession,
  loading,
  onSelect,
  onAddWorkspace,
  onRemoveWorkspace,
  onNewSession,
  teamEnabled = false,
  teamLoggedIn = false,
  teamUsername = null,
  teamTopics = [],
  teamBindings = [],
  teamActiveTopic = null,
  onTeamLogin,
  onTeamLogout,
  onTeamBindSession,
  onTeamSelectTopic,
}: SessionSidebarProps) {
  const total = groups.reduce((n, g) => n + g.rows.length, 0) + ungrouped.length

  return (
    <aside className="gc-side" aria-label="工作区与会话">
      <div className="gc-brand">
        <span>💬 Agent 群聊</span>
      </div>

      <button type="button" className="gc-new-chat" onClick={onAddWorkspace}>
        ＋ 添加工作区
      </button>

      <div className="gc-side-scroll">
        <div className="gc-side-group">工作区</div>
        {groups.length === 0 && (
          <div className="gc-side-empty">
            还没有工作区。
            <br />
            先「添加工作区」选一个工作目录。
          </div>
        )}
        {groups.map((g) => (
          <div key={g.root} className="gc-ws">
            <div className="gc-ws-head" title={g.root}>
              <span className="gc-ws-name">📂 {g.label}</span>
              <button
                type="button"
                className="gc-ws-action"
                title={`在 ${g.label} 创建会话`}
                onClick={() => onNewSession(g.root)}
              >
                ＋
              </button>
              {g.removable && (
                <button
                  type="button"
                  className="gc-ws-action gc-ws-action--danger"
                  title={`移除工作区 ${g.label}（不影响目录本身）`}
                  onClick={() => onRemoveWorkspace(g.root)}
                >
                  ✕
                </button>
              )}
            </div>
            {g.rows.length === 0 ? (
              <div className="gc-ws-empty">没有会话，点 ＋ 创建</div>
            ) : (
              g.rows.map((row) => (
                <SessionItem
                  key={row.name}
                  row={row}
                  active={row.name === activeSession}
                  onSelect={() => onSelect(row.name)}
                />
              ))
            )}
          </div>
        ))}

        {ungrouped.length > 0 && (
          <div className="gc-ws">
            <div className="gc-side-group">未分组</div>
            {ungrouped.map((row) => (
              <SessionItem
                key={row.name}
                row={row}
                active={row.name === activeSession}
                onSelect={() => onSelect(row.name)}
              />
            ))}
          </div>
        )}

        {loading && total === 0 && groups.length > 0 && (
          <div className="gc-side-empty">会话加载中…</div>
        )}

        {/* Cockpit 4.0: 团队区（仅当配置 Team Hub 时显示） */}
        {teamEnabled && (
          <TeamZoneInline
            loggedIn={teamLoggedIn}
            username={teamUsername}
            topics={teamTopics}
            bindings={teamBindings}
            localSessions={groups.flatMap((g) => g.rows).map((r) => ({ name: r.name, label: r.name }))}
            activeTopic={teamActiveTopic}
            onLogin={onTeamLogin || (async () => {})}
            onLogout={onTeamLogout || (async () => {})}
            onBindSession={onTeamBindSession || (async () => {})}
            onSelectTopic={onTeamSelectTopic || (() => {})}
          />
        )}
      </div>

      <div className="gc-side-foot">
        <Link className="gc-side-foot-link" to={routes.settings()}>
          ⚙ 设置
        </Link>
      </div>
    </aside>
  )
}

// Cockpit 4.0: 团队区内联组件
function TeamZoneInline({
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
}: {
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
}) {
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
        <div className="gc-ws gc-team-zone">
          <div className="gc-side-group">团队</div>
          <button
            type="button"
            className="gc-team-login-btn"
            onClick={() => setShowLogin(true)}
          >
            登录团队账号
          </button>
        </div>
      )
    }

    return (
      <div className="gc-ws gc-team-zone">
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
    <div className="gc-ws gc-team-zone">
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
        <div className="gc-ws-empty">还没有加入任何 topic</div>
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
              className={`gc-session${
                activeTopic === topic.slug ? ' is-active' : ''
              }${!isBound ? ' is-unbound' : ''}`}
              onClick={() => isBound && onSelectTopic(topic.slug)}
              disabled={!isBound}
              title={
                isBound
                  ? `打开 ${topic.name}（绑定到 ${binding.session}${bindingIsLive ? '' : '，已停止'}）`
                  : `${topic.name}（需要先绑定本机 Session）`
              }
            >
              <span className="gc-session-name">{topic.name}</span>
              {isBound && (
                <span className="gc-session-status">
                  → {binding.session}{bindingIsLive ? '' : '（已停止）'}
                </span>
              )}
              {!isBound && (
                <span className="gc-session-status" style={{ opacity: 0.5 }}>
                  未绑定
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
