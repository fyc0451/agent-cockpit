import { Link, Navigate } from 'react-router-dom'
import { useEnvCheck, useOverview } from '../api/hooks'
import { routes } from '../app/routes'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag } from '../components/Tag'

export function WelcomePage() {
  const overview = useOverview()
  const envCheck = useEnvCheck()

  // 已有项目：空 profile 引导不适用，回第一屏
  if (overview.data?.data.projects?.length) {
    return <Navigate to={routes.overview()} replace />
  }

  // Legacy env-check shape: { herdr: {installed, path}, agents: {...}, agent_mail: {...} }
  const ec = envCheck.data?.data
  const checks: { name: string; ok: boolean }[] = []
  if (ec) {
    for (const key of ['herdr'] as const) {
      const item = ec[key]
      if (item && typeof item === 'object' && 'installed' in item) {
        checks.push({ name: key, ok: (item as { installed: boolean }).installed })
      }
    }
    // agent_mail uses .available (not .installed)
    const mailItem = ec.agent_mail
    if (mailItem && typeof mailItem === 'object' && 'available' in mailItem) {
      checks.push({ name: 'agent_mail', ok: (mailItem as { available: boolean }).available })
    }
    if (ec.agents && typeof ec.agents === 'object') {
      for (const [name, item] of Object.entries(ec.agents as Record<string, unknown>)) {
        if (item && typeof item === 'object' && 'installed' in item) {
          checks.push({ name, ok: (item as { installed: boolean }).installed })
        }
      }
    }
  }
  const failed = checks.filter((c) => !c.ok).length

  return (
    <>
      <PageHeader title="欢迎使用 Cockpit" sub="完成初始设置后，这里会聚合你所有项目的待办" />
      <section className="panel">
        <h2 className="panel-title">环境自检摘要</h2>
        {envCheck.isPending ? (
          <StatusState kind="loading" title="正在检查环境…" />
        ) : envCheck.isError ? (
          <QueryErrorState error={envCheck.error} onRetry={() => envCheck.refetch()} />
        ) : checks.length === 0 ? (
          <StatusState kind="empty" title="暂无检查结果" />
        ) : (
          <div className="doctor-summary">
            <Tag tone={failed > 0 ? 'warning' : 'success'}>
              {failed > 0 ? `${failed} 项需要关注` : '全部通过'}
            </Tag>
            <span className="list-sub">共 {checks.length} 项检查</span>
          </div>
        )}
        <div className="state-actions">
          <Link className="btn btn--primary" to={routes.settings()}>
            前往设置
          </Link>
        </div>
      </section>
      {overview.isError ? (
        <StatusState
          kind="degraded"
          banner
          title="Overview 数据不可用"
          description="项目聚合数据暂时无法加载，不影响初始设置。"
        />
      ) : null}
    </>
  )
}
