import { useEffect, useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useProjectRegistryList } from '../api/registry'
import { useWorkspaceList, workspaceLocation } from '../api/localSlice'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { useSelection } from '../state/selection'

function NotFound({ slug }: { slug: string }) {
  return (
    <StatusState
      kind="error"
      title="项目不存在"
      description={`未找到项目「${slug}」，它可能尚未注册或已被移除。`}
      children={
        <div className="state-actions">
          <Link className="btn btn--primary" to={routes.projects()}>
            查看项目列表
          </Link>
        </div>
      }
    />
  )
}

/**
 * 进入 project scope 的 URL 时用 Registry 权威解析 slug → project：
 * 加载中→loading；接口错误→映射状态；slug 不在列表/列表不完整→typed error（不用 empty）；
 * 命中→写入 selection Context（附 persisted workspaces，供冻结 shell 的 switcher/rail 消费）。
 * 不再调用 legacy /api/projects/{slug}；URL 继续用 slug。
 */
export function ProjectScope({
  slug,
  children,
}: {
  slug: string
  children: (project: Project) => ReactNode
}) {
  const { setProjectScope } = useSelection()
  const list = useProjectRegistryList()
  const resolved = useMemo(
    () => list.data?.data.items.find((p) => p.slug === slug) ?? null,
    [list.data, slug],
  )
  const ws = useWorkspaceList(resolved?.project_id ?? null, resolved?.slug ?? null)

  const project: Project | null = useMemo(() => {
    if (!resolved) return null
    const workspaces: Workspace[] | undefined = ws.data?.data.items.map((w) => ({
      id: w.workspace_id,
      workspace_id: w.workspace_id,
      name: w.name,
      location: workspaceLocation(w),
      status: w.lifecycle,
      isolation_kind: w.isolation_kind,
      lifecycle: w.lifecycle,
      version: w.version,
    }))
    const base: Project = {
      slug: resolved.slug,
      name: resolved.display_name,
      project_id: resolved.project_id,
    }
    if (workspaces !== undefined) base.workspaces = workspaces
    return base
  }, [resolved, ws.data])

  useEffect(() => {
    setProjectScope(project)
  }, [project, setProjectScope])

  if (list.isPending) return <StatusState kind="loading" title="正在加载项目…" />
  if (list.isError) {
    if (list.error instanceof ApiError && list.error.code === 'not_found') {
      // Registry 列表本身 404：与 slug 未命中同语义（deeplink 兼容），保留原始 message
      return (
        <StatusState
          kind="error"
          title="项目不存在"
          description={list.error.message || `未找到项目「${slug}」，它可能尚未注册或已被移除。`}
          children={
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.projects()}>
                查看项目列表
              </Link>
            </div>
          }
        />
      )
    }
    return <QueryErrorState error={list.error} onRetry={() => list.refetch()} />
  }
  if (!resolved || !project) {
    // 列表分页游标出现且 slug 未命中 → 无法确认存在性，不得当 empty/404 谎报
    if (list.data?.data.next_cursor != null) {
      return (
        <StatusState
          kind="error"
          title="项目列表不完整，无法确认项目"
          description={`项目列表存在更多分页（本轮不翻页），无法确认「${slug}」是否已注册。`}
        />
      )
    }
    return <NotFound slug={slug} />
  }
  return <>{children(project)}</>
}
