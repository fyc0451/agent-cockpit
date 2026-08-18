// 左栏（3080 形态）：工作区列表（= 用户添加的工作目录），工作区下挂会话（群聊）。
// 第一步「＋ 添加工作区」选目录；第二步在工作区组头「＋」创建会话。

import { Link } from 'react-router-dom'
import { routes } from '../../app/routes'
import { statusMeta, type SessionRow } from './model'

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
      </div>

      <div className="gc-side-foot">
        <Link className="gc-side-foot-link" to={routes.settings()}>
          ⚙ 设置
        </Link>
      </div>
    </aside>
  )
}
