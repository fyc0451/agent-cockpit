import { Link } from 'react-router-dom'
import { useOverview } from '../api/hooks'
import { routeHrefs, routes } from '../app/routes'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag } from '../components/Tag'

export function ProjectsPage() {
  const overview = useOverview()

  if (overview.isPending) return <StatusState kind="loading" title="正在加载项目列表…" />
  if (overview.isError)
    return <QueryErrorState error={overview.error} onRetry={() => overview.refetch()} />

  const projects = overview.data?.data.projects

  return (
    <>
      <PageHeader title="项目" sub="选择项目进入工作台" />
      {projects === undefined ? (
        <StatusState
          kind="forbidden"
          title="项目列表不可用"
          reason="Project Registry 列表 API 未接通（W1）"
          docsRoute={routeHrefs.doctor()}
        />
      ) : projects.length === 0 ? (
        <StatusState
          kind="empty"
          title="还没有项目"
          description="完成初始设置后，项目会出现在这里。"
          children={
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.welcome()}>
                查看引导
              </Link>
            </div>
          }
        />
      ) : (
        <ul className="list">
          {projects.map((p) => (
            <li key={p.slug ?? p.name} className="list-row">
              <div className="list-row-main">
                <Link
                  className="ellipsis list-title list-link"
                  to={p.slug ? routes.project.workbench(p.slug) : routes.projects()}
                >
                  {p.name ?? p.slug ?? '未命名项目'}
                </Link>
                {p.path ? <span className="ellipsis list-sub">{p.path}</span> : null}
              </div>
              {p.branch ? <Tag tone="neutral">{p.branch}</Tag> : null}
              <Tag tone="neutral">{`${p.workspaces?.length ?? 0} workspaces`}</Tag>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
