import { useParams } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { routeHrefs } from '../app/routes'
import { useCapability, workspaceScope } from '../state/capabilities'

/**
 * W1 文件页：files.read 默认关闭 → forbidden 整页，**不发出任何 /api/files 请求**
 * （禁止回退全局 legacy /api/files/roots|search）。server 权威值声明可用后，
 * 也只渲染占位空态，等 Workspace 文件门面 UI 接入。
 */
function FilesBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const cap = useCapability(
    'files.read',
    workspaceScope(project.slug ?? '', workspace.id ?? ''),
  )

  return (
    <>
      <PageHeader title="文件" sub={`${workspace.name ?? workspace.id}`} />
      {cap.available ? (
        <StatusState
          kind="empty"
          title="文件浏览待接入"
          description="服务端已声明 files.read 可用，Workspace 文件门面 UI 将在后续迭代接入。"
        />
      ) : (
        <StatusState
          kind="forbidden"
          title="文件浏览暂不可用"
          reason={cap.reason}
          docsRoute={cap.docsRoute ?? routeHrefs.doctor()}
        />
      )}
    </>
  )
}

export function FilesPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <FilesBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
