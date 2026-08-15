import { Link, useParams } from 'react-router-dom'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import {
  projectScope,
  useCapability,
  workspaceScope,
  type CapabilityKey,
} from '../state/capabilities'

/** project scope 下未接通能力整页：保留页面头 + forbidden 态 + 返回项目概览 */
export function UnavailableProjectPage({
  title,
  sub,
  capKey,
}: {
  title: string
  sub: string
  capKey: CapabilityKey
}) {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  const cap = useCapability(capKey, projectScope(projectSlug!))
  return (
    <ProjectScope slug={projectSlug!}>
      {() => (
        <>
          <PageHeader title={title} sub={sub} />
          <StatusState kind="forbidden" title="该能力暂不可用" reason={cap.reason}>
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.project.workbench(projectSlug!)}>
                返回项目概览
              </Link>
            </div>
          </StatusState>
        </>
      )}
    </ProjectScope>
  )
}

function UnavailableWorkspaceBody({
  project,
  workspace,
  title,
  sub,
  capKey,
}: {
  project: Project
  workspace: Workspace
  title: string
  sub: string
  capKey: CapabilityKey
}) {
  const cap = useCapability(
    capKey,
    workspaceScope(project.slug ?? '', workspace.id ?? ''),
  )
  return (
    <>
      <PageHeader title={title} sub={sub} />
      <StatusState kind="forbidden" title="该能力暂不可用" reason={cap.reason}>
        <div className="state-actions">
          <Link
            className="btn btn--primary"
            to={routes.workspace.home(project.slug ?? '', workspace.id ?? '')}
          >
            返回工作空间
          </Link>
        </div>
      </StatusState>
    </>
  )
}

/** workspace scope 下未接通能力整页 */
export function UnavailableWorkspacePage({
  title,
  sub,
  capKey,
}: {
  title: string
  sub: string
  capKey: CapabilityKey
}) {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => (
            <UnavailableWorkspaceBody
              project={project}
              workspace={workspace}
              title={title}
              sub={sub}
              capKey={capKey}
            />
          )}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
