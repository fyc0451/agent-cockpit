import { useParams } from 'react-router-dom'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import type { Project, Workspace } from '../api/types'
import {
  projectScope,
  useCapability,
  workspaceScope,
  type CapabilityKey,
} from '../state/capabilities'

/** project scope 下未接通能力整页：保留页面头 + forbidden 态 + 真实原因 + 文档入口 */
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
          <StatusState kind="forbidden" title="该能力暂不可用" reason={cap.reason} docsRoute={cap.docsRoute} />
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
      <StatusState
        kind="forbidden"
        title="该能力暂不可用"
        reason={cap.reason}
        docsRoute={cap.docsRoute}
      />
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
