import { useEffect, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useProject } from '../api/hooks'
import type { Project } from '../api/types'
import { routes } from '../app/routes'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { useSelection } from '../state/selection'

/**
 * 进入 project scope 的 URL 时校验 project 存在性：
 * 加载中→loading；接口错误→映射状态；404/载荷无 slug→typed error（项目不存在，不用 empty）；
 * 存在→写入 selection Context。
 */
export function ProjectScope({
  slug,
  children,
}: {
  slug: string
  children: (project: Project) => ReactNode
}) {
  const { setProjectScope } = useSelection()
  const q = useProject(slug)
  const project = q.data?.data ?? null

  useEffect(() => {
    setProjectScope(project && project.slug ? project : null)
  }, [project, setProjectScope])

  if (q.isPending) return <StatusState kind="loading" title="正在加载项目…" />
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.code === 'not_found') {
      return (
        <StatusState
          kind="error"
          title="项目不存在"
          description={q.error.message || `未找到项目「${slug}」，它可能尚未注册或已被移除。`}
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
    return <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
  }
  if (!project || !project.slug) {
    // 载荷里没有这个 project（等价 not_found）→ typed error，不得用 empty
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
  return <>{children(project)}</>
}
