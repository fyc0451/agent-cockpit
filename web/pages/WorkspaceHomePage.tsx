import { useParams } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { FocusConversation } from '../features/workspace-work/FocusConversation'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'

function WorkspaceBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const projectId = project.project_id ?? ''
  const workspaceId = workspace.workspace_id ?? workspace.id ?? ''

  return (
    <section className="workspace-focus" aria-label="工作对话">
      <header className="workspace-focus-heading">
        <p>{project.name ?? project.slug}</p>
        <h1>{workspace.name && workspace.name !== '' ? workspace.name : '工作空间'}</h1>
      </header>
      <FocusConversation
        key={`${projectId}/${workspaceId}`}
        projectId={projectId}
        workspaceId={workspaceId}
        projectSlug={project.slug ?? ''}
        workspaceRouteId={workspaceId}
      />
    </section>
  )
}

export function WorkspaceHomePage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <WorkspaceBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
