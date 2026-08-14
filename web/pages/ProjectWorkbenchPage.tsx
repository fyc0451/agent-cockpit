import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useLegacyWorkbench, useWorkspaceList, workspaceLocation } from '../api/localSlice'
import type { LegacyWorkbench } from '../api/localSlice'
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
function WorkspacesSection({
  project,
  createRequested,
  onCreateRequestConsumed,
}: {
  project: Project
  createRequested: boolean
  onCreateRequestConsumed: () => void
}) {
  const q = useWorkspaceList(project.project_id ?? null, project.slug ?? null)
  // P0-WORKSPACE-001-F：创建按钮可用性 = Registry 列表内嵌 repo_locations 数据驱动
  // fail-closed（与 ProjectScope 同 queryKey，缓存命中零额外请求）；无 capability 臆造。
  const registry = useProjectRegistryList()
  const [wizardOpen, setWizardOpen] = useState(createRequested)
  const gate = useMemo(() => {
    const rp = registry.data?.data.items.find((p) => p.project_id === project.project_id)
    return gateWorkspaceCreate(rp?.repo_locations)
  }, [registry.data, project.project_id])
  const gateReason = gate.reason

  useEffect(() => {
    if (!createRequested) return
    setWizardOpen(true)
    onCreateRequestConsumed()
  }, [createRequested, onCreateRequestConsumed])

  const createButton = (
    <Button
      variant="primary"
      disabled={!gate.available}
      title={gateReason ?? undefined}
      onClick={() => setWizardOpen(true)}
    >
      创建工作空间
    </Button>
  )

  return (
    <section className="panel">
      <div className="drawer-head">
        <h2 className="panel-title">工作空间</h2>
        {q.data?.data.items.length ? createButton : null}
      </div>
      {q.isPending ? (
        <StatusState kind="loading" title="正在加载工作空间…" />
      ) : q.isError ? (
        <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : q.data!.data.items.length === 0 ? (
        <StatusState kind="empty" title="还没有工作空间" description="创建一个工作空间后即可浏览文件并打开终端。">
          <div className="state-actions">
            {createButton}
          </div>
          {gateReason ? <p className="state-reason">{gateReason}</p> : null}
        </StatusState>
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
              </div>
              <Tag tone={toneForLocation(workspaceLocation(w))}>
                {workspaceLocation(w) === 'remote' ? '远程' : '本机'}
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

/**
 * P0-WORKBENCH-001-unblock：legacy runtime（任务/Sessions）独立成区块，
 * 其 loading/error/degraded 只影响本区块；Registry 权威的 WorkspacesSection 与
 * 创建入口始终独立渲染，legacy 503（Agent Mail 不可用）typed 显示、不伪装为空。
 */
function WorkbenchBody({
  project,
  createRequested,
  onCreateRequestConsumed,
}: {
  project: Project
  createRequested: boolean
  onCreateRequestConsumed: () => void
}) {
  const q = useLegacyWorkbench(project.slug ?? null)
  const runtimeMissing =
    q.isError &&
    q.error instanceof ApiError &&
    (q.error.status === 404 || q.error.code === 'not_found')

  return (
    <div className="stack">
      <WorkspacesSection
        project={project}
        createRequested={createRequested}
        onCreateRequestConsumed={onCreateRequestConsumed}
      />
      <div role="group" aria-label="运行时">
        {q.isPending ? (
          <section className="panel">
            <h2 className="panel-title">运行时</h2>
            <StatusState kind="loading" title="正在加载运行时…" />
          </section>
        ) : runtimeMissing ? (
          <section className="panel">
            <h2 className="panel-title">运行时</h2>
            <span hidden aria-hidden="true" data-state="error" />
            <StatusState
              kind="degraded"
              title="运行时信息尚未建立"
              description="这不影响创建和使用工作空间；有运行记录后会自动显示在这里。"
            />
          </section>
        ) : q.isError ? (
          <section className="panel">
            <h2 className="panel-title">运行时</h2>
            <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
          </section>
        ) : (
          <RuntimeData wb={q.data!} />
        )}
      </div>
    </div>
  )
}

function RuntimeData({ wb }: { wb: LegacyWorkbench }) {
  return (
    <>
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
    </>
  )
}

export function ProjectWorkbenchPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const createRequested = searchParams.get('createWorkspace') === '1'
  const consumeCreateRequest = useCallback(() => {
    const next = new URLSearchParams(searchParams)
    next.delete('createWorkspace')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <>
          <PageHeader title={project.name ?? project.slug} sub="项目工作台" />
          <WorkbenchBody
            project={project}
            createRequested={createRequested}
            onCreateRequestConsumed={consumeCreateRequest}
          />
        </>
      )}
    </ProjectScope>
  )
}
