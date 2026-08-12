import type { ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import { useWorkbench } from '../api/hooks'
import type { Project } from '../api/types'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, toneForStatus } from '../components/Tag'
import { ProjectScope } from '../features/ProjectScope'

function Section({
  title,
  value,
  renderItems,
}: {
  title: string
  value: unknown
  renderItems: (items: Record<string, unknown>[]) => ReactNode
}) {
  // 区块级 partial 态：字段缺失 → degraded；空数组 → empty；有数据 → 渲染
  if (value === undefined || value === null) {
    return (
      <section className="panel">
        <h2 className="panel-title">{title}</h2>
        <StatusState kind="degraded" title="该区块数据未提供" description="后端载荷中缺少此字段。" />
      </section>
    )
  }
  const items = Array.isArray(value) ? (value as Record<string, unknown>[]) : []
  return (
    <section className="panel">
      <h2 className="panel-title">{title}</h2>
      {items.length === 0 ? <StatusState kind="empty" title="暂无数据" /> : renderItems(items)}
    </section>
  )
}

function itemText(item: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = item[k]
    if (typeof v === 'string' && v) return v
  }
  return ''
}

function WorkbenchBody({ project }: { project: Project }) {
  const q = useWorkbench(project.slug ?? null)

  if (q.isPending) return <StatusState kind="loading" title="正在加载工作台…" />
  if (q.isError) return <QueryErrorState error={q.error} onRetry={() => q.refetch()} />

  const wb = q.data?.data ?? {}

  return (
    <div className="stack">
      <Section
        title="Agents"
        value={wb.agents}
        renderItems={(items) => (
          <ul className="list">
            {items.map((a, i) => (
              <li key={i} className="list-row">
                <span className="ellipsis list-title">{itemText(a, ['name', 'id', 'title']) || 'agent'}</span>
                {typeof a.status === 'string' ? <Tag tone={toneForStatus(a.status)}>{a.status}</Tag> : null}
              </li>
            ))}
          </ul>
        )}
      />
      <Section
        title="任务"
        value={wb.tasks}
        renderItems={(items) => (
          <ul className="list">
            {items.map((t, i) => (
              <li key={i} className="list-row">
                <span className="ellipsis list-title">{itemText(t, ['title', 'id']) || 'task'}</span>
                {typeof t.status === 'string' ? <Tag tone={toneForStatus(t.status)}>{t.status}</Tag> : null}
              </li>
            ))}
          </ul>
        )}
      />
      <Section
        title="最近活动"
        value={wb.activity}
        renderItems={(items) => (
          <ul className="list">
            {items.map((a, i) => (
              <li key={i} className="list-row">
                <span className="ellipsis list-title">
                  {itemText(a, ['title', 'summary', 'message']) || 'activity'}
                </span>
              </li>
            ))}
          </ul>
        )}
      />
    </div>
  )
}

export function ProjectWorkbenchPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <>
          <PageHeader
            title={project.name ?? project.slug}
            sub={project.branch ? `branch ${project.branch}` : '项目工作台'}
          />
          <WorkbenchBody project={project} />
        </>
      )}
    </ProjectScope>
  )
}
