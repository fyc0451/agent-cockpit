import { Link } from 'react-router-dom'
import { useAttention, useOverview } from '../api/hooks'
import { attentionItems, degradedSources, isDegraded } from '../api/normalize'
import { routes } from '../app/routes'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, toneForStatus } from '../components/Tag'

export function OverviewPage() {
  const overview = useOverview()
  const attention = useAttention()

  if (overview.isPending && attention.isPending) {
    return <StatusState kind="loading" title="正在汇总工作…" />
  }
  // 两个源都失败才整页报错；只有一个失败时按 degraded 渲染另一个
  if (overview.isError && attention.isError) {
    return <QueryErrorState error={overview.error} onRetry={() => overview.refetch()} />
  }

  const items = attentionItems(attention.data?.data)
  const meta = attention.data?.meta ?? overview.data?.meta
  const badSources = degradedSources(meta)
  const degraded =
    isDegraded(meta) || overview.isError || attention.isError

  return (
    <>
      <PageHeader title="需要你处理" sub="跨项目聚合的待决定、待回复与提醒" />
      {degraded ? (
        <StatusState
          kind="degraded"
          banner
          title="部分数据源不可用"
          description={
            badSources.length > 0
              ? badSources
                  .map((s) => `${s.name ?? 'unknown'}：${s.reason ?? s.status ?? '不可用'}`)
                  .join('；')
              : '部分数据源暂不可用，以下结果可能不完整。'
          }
        />
      ) : null}
      {items.length === 0 ? (
        <StatusState
          kind="empty"
          title="还没有可汇总的工作"
          description="连接项目与 Runtime 后，待决定事项会聚合到这里。"
          children={
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.settings()}>
                开始设置
              </Link>
            </div>
          }
        />
      ) : (
        <ul className="list">
          {items.map((item, i) => (
            <li key={item.id ?? i} className="list-row">
              <div className="list-row-main">
                <span className="ellipsis list-title">{item.title ?? '未命名事项'}</span>
                {item.summary ? (
                  <span className="ellipsis list-sub">{item.summary}</span>
                ) : null}
              </div>
              {item.project ? <Tag tone="neutral">{item.project}</Tag> : null}
              {item.kind ? <Tag tone="accent">{item.kind}</Tag> : null}
              {item.status ? <Tag tone={toneForStatus(item.status)}>{item.status}</Tag> : null}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
