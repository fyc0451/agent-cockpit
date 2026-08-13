import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useLegacyWorkbench, useWorkspaceList, workspaceLocation } from '../api/localSlice'
import { useProjectRegistryList } from '../api/registry'
import { gateWorkspaceCreate } from '../api/workspaceCreate'
import type { Project } from '../api/types'
import { routes } from '../app/routes'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, toneForLocation, toneForStatus } from '../components/Tag'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceWizard } from '../features/workspace-wizard/WorkspaceWizard'

function text(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

/** persisted workspaces 深链列表（Registry 权威，非 fixture 嵌入数组） */
function WorkspacesSection({ project }: { project: Project }) {
  const q = useWorkspaceList(project.project_id ?? null, project.slug ?? null)
  // P0-WORKSPACE-001-F：创建按钮可用性 = Registry 列表内嵌 repo_locations 数据驱动
  // fail-closed（与 ProjectScope 同 queryKey，缓存命中零额外请求）；无 capability 臆造。
  const registry = useProjectRegistryList()
  const [wizardOpen, setWizardOpen] = useState(false)
  const gate = useMemo(() => {
    const rp = registry.data?.data.items.find((p) => p.project_id === project.project_id)
    return gateWorkspaceCreate(rp?.repo_locations)
  }, [registry.data, project.project_id])
  return (
    <section className="panel">
      <div className="drawer-head">
        <h2 className="panel-title">Workspaces</h2>
        <Button
          variant="primary"
          disabled={!gate.available}
          title={gate.reason ?? undefined}
          onClick={() => setWizardOpen(true)}
        >
          创建 Workspace
        </Button>
      </div>
      {q.isPending ? (
        <StatusState kind="loading" title="正在加载 Workspaces…" />
      ) : q.isError ? (
        <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data!.data.items.length === 0 ? (
        <StatusState kind="empty" title="暂无 Workspace" description="该项目还没有持久化的 Workspace。" />
      ) : (
        <ul className="list">
          {q.data!.data.items.map((w) => (
            <li key={w.workspace_id} className="list-row">
              <div className="list-row-main">
                <Link
                  className="ellipsis list-title list-link"
                  to={routes.workspace.home(project.slug ?? '', w.workspace_id)}
                >
                  {w.name}
                </Link>
                <span className="ellipsis list-sub">{w.workspace_id}</span>
              </div>
              <Tag tone={toneForLocation(workspaceLocation(w))}>
                {workspaceLocation(w) === 'remote' ? '远程' : '本机 Local'}
              </Tag>
            </li>
          ))}
        </ul>
      )}
      <WorkspaceWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        projectSlug={project.slug ?? ''}
        projectId={project.project_id ?? ''}
        repos={gate.eligible}
      />
    </section>
  )
}

function WorkbenchBody({ project }: { project: Project }) {
  const q = useLegacyWorkbench(project.slug ?? null)

  if (q.isPending) return <StatusState kind="loading" title="正在加载工作台…" />
  if (q.isError) return <QueryErrorState error={q.error} onRetry={() => q.refetch()} />

  const wb = q.data!

  return (
    <div className="stack">
      {wb.source.degraded ? (
        <StatusState
          kind="degraded"
          banner
          title="运行时数据源不可用"
          description="Herdr 快照不可用，session 列表已降级为空（非真实为空）。"
        />
      ) : null}
      <section className="panel">
        <h2 className="panel-title">任务</h2>
        {wb.assignments.length === 0 ? (
          <StatusState kind="empty" title="暂无任务" />
        ) : (
          <ul className="list">
            {wb.assignments.map((a, i) => (
              <li key={text(a.assignment_id) || i} className="list-row">
                <div className="list-row-main">
                  <span className="ellipsis list-title">{text(a.assignment) || 'assignment'}</span>
                  {text(a.assignee) ? (
                    <span className="ellipsis list-sub">{text(a.assignee)}</span>
                  ) : null}
                </div>
                {text(a.status) ? <Tag tone={toneForStatus(text(a.status))}>{text(a.status)}</Tag> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="panel">
        <h2 className="panel-title">Sessions</h2>
        {wb.sessions.length === 0 ? (
          wb.source.degraded ? (
            <StatusState kind="degraded" title="Session 列表不可用" description="运行时快照降级，无法确认是否有 session。" />
          ) : (
            <StatusState kind="empty" title="暂无 session" />
          )
        ) : (
          <ul className="list">
            {wb.sessions.map((s) => (
              <li key={s.session} className="list-row">
                <div className="list-row-main">
                  <span className="ellipsis list-title">{s.session}</span>
                  {s.panes.length > 0 ? (
                    <span className="ellipsis list-sub">
                      {s.panes.map((p) => text(p.agent)).filter(Boolean).join('、')}
                    </span>
                  ) : null}
                </div>
                {text(s.status) ? <Tag tone={toneForStatus(text(s.status))}>{text(s.status)}</Tag> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      <WorkspacesSection project={project} />
    </div>
  )
}

export function ProjectWorkbenchPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <>
          <PageHeader title={project.name ?? project.slug} sub="项目工作台" />
          <WorkbenchBody project={project} />
        </>
      )}
    </ProjectScope>
  )
}
