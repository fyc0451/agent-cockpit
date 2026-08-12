import { useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { routes } from '../app/routes'
import { StatusState } from '../components/StatusState'
import { Tag, toneForLocation } from '../components/Tag'
import { useDialog } from '../components/useDialog'
import { useSelection } from '../state/selection'

export function WorkspaceSwitcher({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLElement>(null)
  const navigate = useNavigate()
  const { projectSlug, project } = useSelection()
  const close = useCallback(onClose, [onClose])
  useDialog(ref, open, close)

  if (!open || !projectSlug) return null
  const workspaces = project?.workspaces ?? []

  return (
    <div className="overlay" onClick={close}>
      <section
        ref={ref}
        className="modal modal--switcher"
        role="dialog"
        aria-modal="true"
        aria-label="Workspace 切换"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2 className="panel-title">切换 Workspace</h2>
          <button type="button" className="btn btn--icon" aria-label="关闭" onClick={close}>
            ×
          </button>
        </div>
        {workspaces.length === 0 ? (
          <StatusState
            kind="empty"
            title="没有可用 Workspace"
            description="当前项目尚未提供 Workspaces 数据。"
          />
        ) : (
          <ul className="drawer-list">
            {workspaces.map((w) => (
              <li key={w.id ?? w.name}>
                <button
                  type="button"
                  className="drawer-item"
                  onClick={() => {
                    if (!w.id) return
                    close()
                    navigate(routes.workspace.home(projectSlug, w.id))
                  }}
                >
                  <span
                    className={`ws-dot ${w.location === 'remote' ? 'ws-dot--remote' : 'ws-dot--local'}`}
                    aria-hidden="true"
                  />
                  <span className="ellipsis drawer-item-name">{w.name ?? w.id}</span>
                  {w.location ? <Tag tone={toneForLocation(w.location)}>{w.location}</Tag> : null}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
