import { Link } from 'react-router-dom'
import { useAttention, useOverview } from '../api/hooks'
import { attentionItems, degradedSources, isDegraded } from '../api/normalize'
import { useProjectRegistryList } from '../api/registry'
import { routes } from '../app/routes'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, toneForStatus } from '../components/Tag'

export function OverviewPage() {
  const overview = useOverview()
  const attention = useAttention()
  const registry = useProjectRegistryList()

  if (overview.isPending && attention.isPending) {
    return <StatusState kind="loading" title="正在汇总工作…" />
  }
  // 两个源都失败才整页报错；只有一个失败时按 degraded 渲染另一个
  if (overview.isError && attention.isError) {
    return <QueryErrorState error={overview.error} onRetry={() => overview.refetch()} />
  }

  const items = attentionItems(attention.data?.data)
  const metas = [overview.data?.meta, attention.data?.meta]
  const badSources = metas.flatMap((meta) => degradedSources(meta))
  const badSourceMessages = [
    ...new Set(
      badSources.map(
        (source) =>
          `${source.name ?? 'unknown'}：${source.reason ?? source.status ?? '不可用'}`,
      ),
    ),
  ]
  const degraded =
    metas.some((meta) => isDegraded(meta)) || overview.isError || attention.isError

  // 首用 CTA 由 Registry 权威门控：零项目时即使有 Doctor/Runtime attention 也优先引导登记
  const noProjects = registry.data?.data.items.length === 0

  return (
    <>
      <PageHeader title="需要你处理" sub="跨项目聚合的待决定、待回复与提醒" />
      {noProjects ? (
        <StatusState
          kind="empty"
          title="还没有项目"
          description="选择一个代码目录登记后，待决定事项会聚合到这里。"
          children={
            <div className="state-actions">
              <Link className="btn btn--primary" to={routes.projects({ wizard: true })}>
                选择代码目录
              </Link>
            </div>
          }
        />
      ) : null}
      {degraded ? (
        <StatusState
          kind="degraded"
          banner
          title="部分数据源不可用"
          description={
            badSourceMessages.length > 0
              ? badSourceMessages.join('；')
              : '部分数据源暂不可用，以下结果可能不完整。'
          }
        />
      ) : null}
      {attention.isError ? (
        // attention 源失败：区块级 partial/degraded，不得显示 empty/0 假成功
        <StatusState
          kind="degraded"
          title="Attention 摘要不可用"
          description="该来源暂不可用，以上列表不完整或暂缺。"
        />
      ) : items.length === 0 ? (
        // empty 只在真无数据（无 degraded）时出现；零项目时上面的首用 CTA 已引导，不重复渲染
        noProjects ? null : degraded ? (
          <StatusState
            kind="degraded"
            title="部分数据不可用"
            description="部分来源失败，当前没有可展示的完整数据。"
          />
        ) : (
          <StatusState
            kind="empty"
            title="还没有可汇总的工作"
            description="待决定、待回复与提醒会聚合到这里。"
          />
        )
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
