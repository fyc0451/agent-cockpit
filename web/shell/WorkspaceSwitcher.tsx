import { useCallback, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Workspace } from '../api/types'
import { isRemoteWorkspace } from '../api/normalize'
import { routes } from '../app/routes'
import { StatusState } from '../components/StatusState'
import { useDialog } from '../components/useDialog'
import { useSelection } from '../state/selection'

function SwitcherItem({
  workspace: w,
  onSelect,
}: {
  workspace: Workspace
  onSelect: (w: Workspace) => void
}) {
  return (
    <li>
      <button type="button" className="drawer-item" onClick={() => onSelect(w)}>
        <span className="ws-dot ws-dot--local" aria-hidden="true" />
        <span className="ellipsis drawer-item-name">{w.name ?? w.id}</span>
      </button>
    </li>
  )
}

export function WorkspaceSwitcher({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const navigate = useNavigate()
  const { projectSlug, project } = useSelection()
  const close = useCallback(onClose, [onClose])
  useDialog(ref, open, close)

  if (!open || !projectSlug) return null
  const workspaces = (project?.workspaces ?? []).filter((workspace) => !isRemoteWorkspace(workspace))

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
            title="还没有本机工作空间"
            description="创建工作空间后即可开始工作对话。"
          >
            <div className="state-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  close()
                  navigate(routes.project.workbench(projectSlug, { createWorkspace: true }))
                }}
              >
                创建工作空间
              </button>
            </div>
          </StatusState>
        ) : (
          <ul className="drawer-list" ref={listRef} onKeyDown={onListKeyDown}>
            {workspaces.map((w) => (
              <SwitcherItem
                key={w.id ?? w.name}
                workspace={w}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
