import { useParams } from 'react-router-dom'
import { useTasks } from '../api/hooks'
import type { Project, Workspace } from '../api/types'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, toneForStatus } from '../components/Tag'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'

function TasksBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const q = useTasks({
    project: project.slug ?? undefined,
    workspace: workspace.id ?? undefined,
  })

  if (q.isPending) return <StatusState kind="loading" title="正在加载任务…" />
  if (q.isError) return <QueryErrorState error={q.error} onRetry={() => q.refetch()} />

  const tasks = q.data?.data.items ?? q.data?.data.tasks ?? []

  return (
    <>
      <PageHeader title="任务" sub={`${workspace.name ?? workspace.id} · 只读`} />
      {tasks.length === 0 ? (
        <StatusState kind="empty" title="暂无任务" description="该工作空间当前没有任务记录。" />
      ) : (
        <ul className="list">
          {tasks.map((t, i) => (
            <li key={t.id ?? i} className="list-row">
              <div className="list-row-main">
                <span className="ellipsis list-title">{t.title ?? t.id ?? '未命名任务'}</span>
                {t.kind ? <span className="ellipsis list-sub">{t.kind}</span> : null}
              </div>
              {t.status ? <Tag tone={toneForStatus(t.status)}>{t.status}</Tag> : null}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

export function TasksPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <TasksBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
