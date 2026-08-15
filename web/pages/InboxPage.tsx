import { Link, useSearchParams } from 'react-router-dom'
import { useAttention } from '../api/hooks'
import { attentionItems, isNeedsAction } from '../api/normalize'
import { useProjectRegistryList } from '../api/registry'
import { routes } from '../app/routes'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tabs, tabId, tabPanelId } from '../components/Tabs'
import { Tag, toneForStatus } from '../components/Tag'

const VIEWS = [
  { key: 'all', label: '全部' },
  { key: 'needs-action', label: '需要你处理' },
] as const

/** 仅在 Registry 中唯一命中 slug 或唯一命中显示名时返回 slug；不推导、不猜。 */
export function resolveInboxProjectSlug(
  label: string | undefined,
  projects: ReadonlyArray<{ slug: string; display_name: string }>,
): string | null {
  if (!label) return null
  const slugHits = projects.filter((p) => p.slug === label)
  if (slugHits.length === 1) return slugHits[0].slug
  if (slugHits.length !== 0) return null
  const nameHits = projects.filter((p) => p.display_name === label)
  return nameHits.length === 1 ? nameHits[0].slug : null
}

export function InboxPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const view = searchParams.get('view') ?? 'all'
  const q = useAttention()
  const registry = useProjectRegistryList()

  const items = attentionItems(q.data?.data)
  const filtered = view === 'needs-action' ? items.filter(isNeedsAction) : items
  const projects = registry.data?.data.items ?? []
  const noProjects = registry.data?.data.items.length === 0

  return (
    <>
      <PageHeader title="提问与回复" sub="来自各项目的待回复与待决定事项" />
      <Tabs
        tabs={VIEWS}
        active={view}
        ariaLabel="Inbox 视图"
        onChange={(key) => setSearchParams(key === 'all' ? {} : { view: key })}
      />
      <div role="tabpanel" id={tabPanelId(view)} aria-labelledby={tabId(view)}>
        {q.isPending ? (
          <StatusState kind="loading" />
        ) : q.isError ? (
          <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
        ) : filtered.length === 0 ? (
          <StatusState
            kind="empty"
            title={view === 'needs-action' ? '没有需要你处理的事项' : '收件箱为空'}
            description={noProjects ? '添加一个项目后，待回复事项会出现在这里。' : '没有待回复事项。可以回到项目继续工作。'}
          >
            <div className="state-actions">
              {noProjects ? (
                <Link className="btn btn--primary" to={routes.projects({ wizard: true })}>
                  选择代码目录
                </Link>
              ) : (
                <Link className="btn btn--primary" to={routes.projects()}>
                  查看项目
                </Link>
              )}
            </div>
          </StatusState>
        ) : (
          <ul className="list">
            {filtered.map((item, i) => {
              const slug = resolveInboxProjectSlug(item.project, projects)
              const title = item.title ?? '未命名事项'
              return (
                <li key={item.id ?? i} className="list-row">
                  <div className="list-row-main">
                    {slug ? (
                      <Link className="ellipsis list-title list-link" to={routes.project.workbench(slug)}>
                        {title}
                      </Link>
                    ) : (
                      <span className="ellipsis list-title">{title}</span>
                    )}
                    {item.summary ? <span className="ellipsis list-sub">{item.summary}</span> : null}
                  </div>
                  {item.project ? <Tag tone="neutral">{item.project}</Tag> : null}
                  {item.kind ? <Tag tone="accent">{item.kind}</Tag> : null}
                  {item.status ? <Tag tone={toneForStatus(item.status)}>{item.status}</Tag> : null}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </>
  )
}
