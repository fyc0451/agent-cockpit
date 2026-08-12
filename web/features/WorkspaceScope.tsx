import { useEffect, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import { StatusState } from '../components/StatusState'
import { useSelection } from '../state/selection'

/** 校验 workspace 存在于当前 project 的 workspaces 中，存在则写入 selection Context */
export function WorkspaceScope({
  project,
  children,
}: {
  project: Project
  children: (workspace: Workspace) => ReactNode
}) {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { setWorkspaceScope } = useSelection()
  const ws =
    (project.workspaces ?? []).find((w) => w.id === workspaceId) ?? null

  useEffect(() => {
    setWorkspaceScope(ws)
  }, [ws, setWorkspaceScope])

  if (!ws) {
    // workspace 不在 project 的 workspaces 列表中（mismatch）→ typed error，不得用 empty
    return (
      <StatusState
        kind="error"
        title="Workspace 不存在或不属于当前项目"
        description={`项目「${project.name ?? project.slug}」下没有 ID 为「${workspaceId}」的 Workspace。`}
        children={
          <div className="state-actions">
            <Link className="btn btn--primary" to={routes.project.workbench(project.slug ?? '')}>
              返回项目工作台
            </Link>
          </div>
        }
      />
    )
  }
  return <>{children(ws)}</>
}
