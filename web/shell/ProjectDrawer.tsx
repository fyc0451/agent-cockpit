import { useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useProjectRegistryList } from '../api/registry'
import { routes } from '../app/routes'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag } from '../components/Tag'
import { useDialog } from '../components/useDialog'

function DrawerBody({
  onNavigate,
  onAddProject,
}: {
  onNavigate: (slug: string) => void
  onAddProject: () => void
}) {
  // Registry 权威（与 ProjectsPage 同一数据源），不再消费 overview 的 legacy projects
  const list = useProjectRegistryList()
  if (list.isPending) return <StatusState kind="loading" title="正在加载项目列表…" />
  if (list.isError)
    return <QueryErrorState error={list.error} onRetry={() => list.refetch()} />
  const data = list.data!.data
  const meta = list.data!.meta
  const degraded =
    meta?.partial === true ||
    data.next_cursor != null ||
    (meta?.sources ?? []).some((s) => s.status != null && s.status !== 'available')
  const projects = data.items.filter((p) => p.lifecycle !== 'archived')
  if (projects.length === 0) {
    return degraded ? (
      <StatusState kind="degraded" title="部分数据不可用" description="列表来源异常，暂无可展示的完整数据。" />
    ) : (
      <StatusState
        kind="empty"
        title="还没有项目"
        description="选择一个代码目录后即可在这里切换项目。"
        children={
          <div className="state-actions">
            <button type="button" className="btn btn--primary" onClick={onAddProject}>
              选择代码目录
            </button>
          </div>
        }
      />
    )
  }
  return (
    <>
      {degraded ? (
        <StatusState
          kind="degraded"
          banner
          title="列表数据不完整"
          description="部分数据源不可用，列表可能不完整。"
        />
      ) : null}
      <ul className="drawer-list">
        {projects.map((p) => (
          <li key={p.project_id}>
            <button
              type="button"
              className="drawer-item"
              onClick={() => onNavigate(p.slug)}
            >
              <span className="ellipsis drawer-item-name">{p.display_name}</span>
              {p.lifecycle !== 'active' ? <Tag tone="neutral">{p.lifecycle}</Tag> : null}
            </button>
          </li>
        ))}
      </ul>
    </>
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
          onAddProject={() => {
            // 先关 drawer 再导航，避免向导在抽屉/overlay 之后打开
            close()
            navigate(routes.projects({ wizard: true }))
          }}
        />
      </aside>
    </div>
  )
}
