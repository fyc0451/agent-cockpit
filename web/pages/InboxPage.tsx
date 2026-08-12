import { useSearchParams } from 'react-router-dom'
import { useAttention } from '../api/hooks'
import { attentionItems, isNeedsAction } from '../api/normalize'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tabs, tabId, tabPanelId } from '../components/Tabs'
import { Tag, toneForStatus } from '../components/Tag'

const VIEWS = [
  { key: 'all', label: '全部' },
  { key: 'needs-action', label: '需要你处理' },
] as const

export function InboxPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const view = searchParams.get('view') ?? 'all'
  const q = useAttention()

  const items = attentionItems(q.data?.data)
  const filtered = view === 'needs-action' ? items.filter(isNeedsAction) : items

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
          />
        ) : (
          <ul className="list">
            {filtered.map((item, i) => (
              <li key={item.id ?? i} className="list-row">
                <div className="list-row-main">
                  <span className="ellipsis list-title">{item.title ?? '未命名事项'}</span>
                  {item.summary ? <span className="ellipsis list-sub">{item.summary}</span> : null}
                </div>
                {item.project ? <Tag tone="neutral">{item.project}</Tag> : null}
                {item.kind ? <Tag tone="accent">{item.kind}</Tag> : null}
                {item.status ? <Tag tone={toneForStatus(item.status)}>{item.status}</Tag> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  )
}
