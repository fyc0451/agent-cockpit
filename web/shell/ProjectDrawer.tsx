import { useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useOverview } from '../api/hooks'
import type { Project } from '../api/types'
import { routeHrefs, routes } from '../app/routes'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag } from '../components/Tag'
import { useDialog } from '../components/useDialog'

function DrawerBody({ onNavigate }: { onNavigate: (slug: string) => void }) {
  const overview = useOverview()
  if (overview.isPending) return <StatusState kind="loading" title="正在加载项目列表…" />
  if (overview.isError)
    return <QueryErrorState error={overview.error} onRetry={() => overview.refetch()} />
  const projects = overview.data?.data.projects
  if (projects === undefined) {
    return (
      <StatusState
        kind="forbidden"
        title="项目列表不可用"
        reason="Project Registry 列表 API 未接通（W1）"
        docsRoute={routeHrefs.doctor()}
      />
    )
  }
  if (projects.length === 0) {
    return <StatusState kind="empty" title="还没有项目" description="完成初始设置后即可在这里切换项目。" />
  }
  return (
    <ul className="drawer-list">
      {projects.map((p: Project) => (
        <li key={p.slug ?? p.name}>
          <button
            type="button"
            className="drawer-item"
            onClick={() => p.slug && onNavigate(p.slug)}
          >
            <span className="ellipsis drawer-item-name">{p.name ?? p.slug}</span>
            {p.branch ? <Tag tone="neutral">{p.branch}</Tag> : null}
          </button>
        </li>
      ))}
    </ul>
  )
}

export function ProjectDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLElement>(null)
  const navigate = useNavigate()
  const close = useCallback(onClose, [onClose])
  useDialog(ref, open, close)

  if (!open) return null
  return (
    <div className="overlay" onClick={close}>
      <aside
        ref={ref}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="项目切换"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2 className="panel-title">切换项目</h2>
          <button type="button" className="btn btn--icon" aria-label="关闭" onClick={close}>
            ×
          </button>
        </div>
        <DrawerBody
          onNavigate={(slug) => {
            close()
            navigate(routes.project.workbench(slug))
          }}
        />
      </aside>
    </div>
  )
}
