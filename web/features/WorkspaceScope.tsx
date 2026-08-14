import { useEffect, useMemo, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useWorkspaceDetail, workspaceLocation } from '../api/localSlice'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { useSelection } from '../state/selection'

/**
 * 用 project_id + URL workspace_id 调真实 Workspace detail：
 * 加载中→loading；404/409/跨项目不匹配→typed error（不用 empty，不从嵌入数组猜身份）；
 * 存在→写入 selection Context。
 */
export function WorkspaceScope({
  project,
  children,
}: {
  project: Project
  children: (workspace: Workspace) => ReactNode
}) {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { setWorkspaceScope } = useSelection()
  const q = useWorkspaceDetail(
    project.project_id ?? null,
    workspaceId ?? null,
    project.slug ?? null,
  )
  const detail = q.data?.data ?? null

  // 跨项目/串号守卫：detail 身份必须与 URL + 当前 project 完全一致
  const mismatch =
    detail != null &&
    (detail.workspace_id !== workspaceId || detail.project_id !== project.project_id)

  // useMemo：新对象字面量每 render 换引用会导致 setWorkspaceScope effect 死循环
  const ws: Workspace | null = useMemo(() => {
    if (!detail || mismatch) return null
    return {
      id: detail.workspace_id,
      workspace_id: detail.workspace_id,
      name: detail.name,
      location: workspaceLocation(detail),
      status: detail.lifecycle,
      isolation_kind: detail.isolation_kind,
      lifecycle: detail.lifecycle,
      goal: detail.goal,
      version: detail.version,
    }
  }, [detail, mismatch])

  useEffect(() => {
    setWorkspaceScope(ws)
  }, [ws, setWorkspaceScope])

  if (q.isPending) return <StatusState kind="loading" title="正在加载工作空间…" />
  if (q.isError || mismatch) {
    const isNotFound =
      mismatch ||
      (q.error instanceof ApiError &&
        (q.error.code === 'not_found' || q.error.code === 'conflict'))
    if (isNotFound) {
      return (
        <StatusState
          kind="error"
          title="工作空间不存在或不属于当前项目"
          description={`项目「${project.name ?? project.slug}」下没有 ID 为「${workspaceId}」的工作空间。`}
          children={
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.project.workbench(project.slug ?? '')}>
                返回项目概览
              </Link>
            </div>
          }
        />
      )
    }
    return <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
  }
  if (!ws) return <StatusState kind="loading" title="正在加载工作空间…" />
  return <>{children(ws)}</>
}
