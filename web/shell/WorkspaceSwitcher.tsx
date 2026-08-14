import { useCallback, useId, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Workspace } from '../api/types'
import { isRemoteWorkspace } from '../api/normalize'
import { routes } from '../app/routes'
import { StatusState } from '../components/StatusState'
import { Tag, toneForLocation } from '../components/Tag'
import { useDialog } from '../components/useDialog'
import { GLOBAL_SCOPE, projectScope, useCapability } from '../state/capabilities'
import { useSelection } from '../state/selection'

function SwitcherItem({
  workspace: w,
  disabled,
  reason,
  onSelect,
}: {
  workspace: Workspace
  disabled: boolean
  reason: string | null
  onSelect: (w: Workspace) => void
}) {
  const descId = useId()
  if (disabled) {
    // Remote fail-closed：可见但 disabled；可聚焦、原因可读（aria-describedby）、激活被拦截（零请求）
    const swallow = (e: { preventDefault: () => void; stopPropagation: () => void }) => {
      e.preventDefault()
      e.stopPropagation()
    }
    return (
      <li>
        <button
          type="button"
          className="drawer-item drawer-item--disabled"
          aria-disabled="true"
          aria-describedby={descId}
          title={reason ?? undefined}
          onClick={swallow}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') swallow(e)
          }}
        >
          <span className="ws-dot ws-dot--remote" aria-hidden="true" />
          <span className="ellipsis drawer-item-name">{w.name ?? w.id}</span>
          {w.location ? <Tag tone={toneForLocation(w.location)}>{w.location}</Tag> : null}
        </button>
        <span id={descId} className="sr-only">
          {w.name ?? w.id}不可用：{reason}
        </span>
      </li>
    )
  }
  return (
    <li>
      <button type="button" className="drawer-item" onClick={() => onSelect(w)}>
        <span
          className={`ws-dot ${w.location === 'remote' ? 'ws-dot--remote' : 'ws-dot--local'}`}
          aria-hidden="true"
        />
        <span className="ellipsis drawer-item-name">{w.name ?? w.id}</span>
        {w.location ? <Tag tone={toneForLocation(w.location)}>{w.location}</Tag> : null}
      </button>
    </li>
  )
}

export function WorkspaceSwitcher({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const navigate = useNavigate()
  const { projectSlug, project } = useSelection()
  const remoteCap = useCapability(
    'remoteHerdr',
    projectSlug ? projectScope(projectSlug) : GLOBAL_SCOPE,
  )
  const close = useCallback(onClose, [onClose])
  useDialog(ref, open, close)

  if (!open || !projectSlug) return null
  const workspaces = project?.workspaces ?? []

  const onSelect = (w: Workspace) => {
    if (!w.id) return
    close()
    navigate(routes.workspace.home(projectSlug, w.id))
  }

  // P2-10：ArrowUp/ArrowDown 循环 roving 焦点，Home/End 跳首尾；Enter 由 button 原生激活；Esc 走 useDialog
  const onListKeyDown = (e: ReactKeyboardEvent<HTMLUListElement>) => {
    const items = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>('.drawer-item') ?? [],
    )
    if (items.length === 0) return
    const idx = items.indexOf(document.activeElement as HTMLElement)
    let next: number | null = null
    if (e.key === 'ArrowDown') next = idx < 0 ? 0 : (idx + 1) % items.length
    else if (e.key === 'ArrowUp') next = idx < 0 ? items.length - 1 : (idx - 1 + items.length) % items.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = items.length - 1
    if (next != null) {
      e.preventDefault()
      e.stopPropagation()
      items[next]?.focus()
    }
  }

  return (
    <div className="overlay" onClick={close}>
      <section
        ref={ref}
        className="modal modal--switcher"
        role="dialog"
        aria-modal="true"
        aria-label="工作空间切换"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2 className="panel-title">切换工作空间</h2>
          <button type="button" className="btn btn--icon" aria-label="关闭" onClick={close}>
            ×
          </button>
        </div>
        {workspaces.length === 0 ? (
          <StatusState
            kind="empty"
            title="没有可用工作空间"
            description="当前项目尚未提供工作空间数据。"
          />
        ) : (
          <ul className="drawer-list" ref={listRef} onKeyDown={onListKeyDown}>
            {workspaces.map((w) => (
              <SwitcherItem
                key={w.id ?? w.name}
                workspace={w}
                disabled={isRemoteWorkspace(w) && !remoteCap.available}
                reason={remoteCap.reason}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
