import { useMemo, useState } from 'react'
import { Navigate, Route, Routes, useParams, useSearchParams } from 'react-router-dom'
import { useWorkspaceList, workspaceLocation } from '../api/localSlice'
import { useProjectRegistryList } from '../api/registry'
import type { Project } from '../api/types'
import { gateWorkspaceCreate } from '../api/workspaceCreate'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceWizard } from '../features/workspace-wizard/WorkspaceWizard'
import { FilesPage } from '../pages/FilesPage'
import { ProjectsPage } from '../pages/ProjectsPage'
import { TerminalPage } from '../pages/TerminalPage'
import { WorkspaceHomePage } from '../pages/WorkspaceHomePage'
import { AppShell } from '../shell/AppShell'
import { loadLastWorkspace } from '../state/workDraft'
import { routePatterns, routes } from './routes'

function ProjectDestination({ project }: { project: Project }) {
  const workspaces = useWorkspaceList(project.project_id ?? null, project.slug ?? null)
  const registry = useProjectRegistryList()
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.get('createWorkspace') === '1'
  const [wizardOpen, setWizardOpen] = useState(requested)
  const gate = useMemo(() => {
    const registered = registry.data?.data.items.find((item) => item.project_id === project.project_id)
    return gateWorkspaceCreate(registered?.repo_locations)
  }, [project.project_id, registry.data])

  if (workspaces.isPending) return <StatusState kind="loading" title="正在加载工作空间…" />
  if (workspaces.isError) {
    return <QueryErrorState error={workspaces.error} onRetry={() => workspaces.refetch()} />
  }

  const localWorkspaces = workspaces.data.data.items.filter((item) => workspaceLocation(item) === 'local')
  if (localWorkspaces.length > 0 && !requested) {
    const last = loadLastWorkspace(project.project_id ?? '')
    const destination = localWorkspaces.find((item) => item.workspace_id === last) ?? localWorkspaces[0]
    return <Navigate to={routes.workspace.home(project.slug ?? '', destination.workspace_id)} replace />
  }

  const closeWizard = () => {
    setWizardOpen(false)
    if (requested) setSearchParams({}, { replace: true })
  }

  return (
    <div className="project-create-state">
      <PageHeader title={project.name ?? project.slug} sub="工作空间" />
      <section className="panel">
        <StatusState
          kind="empty"
          title={localWorkspaces.length === 0 ? '还没有本机工作空间' : '新建工作空间'}
          description="为这个项目建立一个独立的本机工作现场。"
        >
          <div className="state-actions">
            <Button
              variant="primary"
              disabled={!gate.available}
              title={gate.reason ?? undefined}
              onClick={() => setWizardOpen(true)}
            >
              创建工作空间
            </Button>
          </div>
        </StatusState>
      </section>
      <WorkspaceWizard
        open={wizardOpen}
        onClose={closeWizard}
        projectSlug={project.slug ?? ''}
        projectId={project.project_id ?? ''}
        repos={gate.eligible}
      />
    </div>
  )
}

function ProjectEntry() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => <ProjectDestination project={project} />}
    </ProjectScope>
  )
}

function WorkspaceFocusRedirect() {
  const { projectSlug, workspaceId } = useParams<{ projectSlug: string; workspaceId: string }>()
  return <Navigate to={routes.workspace.home(projectSlug!, workspaceId!)} replace />
}

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to={routes.projects()} replace />} />
        <Route path={routePatterns.projects} element={<ProjectsPage />} />
        <Route path={routePatterns.projectWorkbench} element={<ProjectEntry />} />
        <Route path={routePatterns.workspaceBase} element={<WorkspaceHomePage />} />
        <Route path={routePatterns.workspaceFiles} element={<FilesPage />} />
        <Route path={routePatterns.workspaceTerminal} element={<TerminalPage />} />
        <Route path={routePatterns.workspaceAgent} element={<WorkspaceFocusRedirect />} />
        <Route path="*" element={<Navigate to={routes.projects()} replace />} />
      </Routes>
    </AppShell>
  )
}
