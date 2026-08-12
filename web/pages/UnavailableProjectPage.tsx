import { useParams } from 'react-router-dom'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { projectScope, useCapability, type CapabilityKey } from '../state/capabilities'

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
  const cap = useCapability(capKey, projectScope(projectSlug!))
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {() => (
            <>
              <PageHeader title={title} sub={sub} />
              <StatusState
                kind="forbidden"
                title="该能力暂不可用"
                reason={cap.reason}
                docsRoute={cap.docsRoute}
              />
            </>
          )}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
