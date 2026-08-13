import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useProjectRegistryList } from '../api/registry'
import type { RepoLocationSummary } from '../api/registry'
import { routes } from '../app/routes'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tag, type TagTone } from '../components/Tag'
import { ProjectWizard } from '../features/project-wizard/ProjectWizard'

const AVAILABILITY_TONE: Record<string, TagTone> = {
  available: 'success',
  offline: 'warning',
  missing: 'danger',
  unknown: 'neutral',
}

function locationSummary(p: { repo_locations?: RepoLocationSummary[] }): RepoLocationSummary | null {
  return p.repo_locations?.[0] ?? null
}

export function ProjectsPage() {
  const list = useProjectRegistryList()
  const [wizardOpen, setWizardOpen] = useState(false)

  const openWizard = (
    <Button variant="primary" onClick={() => setWizardOpen(true)}>
      添加项目
    </Button>
  )

  // 7 态分离；PageHeader 全程在场（E4：typed error 也不得白屏）
  let body: ReactNode
  if (list.isPending) {
    body = <StatusState kind="loading" title="正在加载项目列表…" />
  } else if (list.isError) {
    // typed error（含 404/ProtocolError/503…）；409 由 code 映射 conflict（预留）
    body = <QueryErrorState error={list.error} onRetry={() => list.refetch()} />
  } else {
    const data = list.data!.data
    const meta = list.data!.meta
    // B3 读/写分离：列表读取只看 GET 数据 + meta.partial/sources（写 capability 与此页无关）。
    // degraded：meta.partial、sources 非 available 或分页游标出现（本轮不翻页）
    const degraded =
      meta?.partial === true ||
      data.next_cursor != null ||
      (meta?.sources ?? []).some((s) => s.status != null && s.status !== 'available')

    body = (
      <>
        {degraded ? (
          <StatusState
            kind="degraded"
            banner
            title="列表数据不完整"
            description={
              data.next_cursor != null
                ? '存在更多项目（分页暂未开放，不会自动翻页）。'
                : '部分数据源不可用，列表可能不完整。'
            }
          />
        ) : null}
        {data.items.length === 0 ? (
          degraded ? (
            <StatusState kind="degraded" title="部分数据不可用" description="列表来源异常，暂无可展示的完整数据。" />
          ) : (
            <StatusState
              kind="empty"
              title="还没有项目"
              description="选择一个本地目录登记为 Project，或先完成初始设置。"
              children={
                <div className="state-actions">
                  <Button variant="primary" onClick={() => setWizardOpen(true)}>
                    选择项目目录
                  </Button>
                  <Link className="btn btn--ghost" to={routes.welcome()}>
                    查看引导
                  </Link>
                </div>
              }
            />
          )
        ) : (
          <ul className="list">
            {/* 表现层排序，合同未定（TBD-6） */}
            {data.items
              .slice()
              .sort((a, b) => a.display_name.localeCompare(b.display_name))
              .map((p) => {
                const loc = locationSummary(p)
                return (
                  <li key={p.project_id} className="list-row">
                    <div className="list-row-main">
                      <Link className="ellipsis list-title list-link" to={routes.project.workbench(p.slug)}>
                        {p.display_name}
                      </Link>
                      {/* B4：只渲染已冻结的 canonical_path，不访问 display_path */}
                      {loc ? <span className="ellipsis list-sub">{loc.canonical_path}</span> : null}
                    </div>
                    {loc ? (
                      <Tag tone={AVAILABILITY_TONE[loc.availability] ?? 'neutral'}>{loc.availability}</Tag>
                    ) : null}
                  </li>
                )
              })}
          </ul>
        )}
      </>
    )
  }

  return (
    <>
      <PageHeader title="项目" sub="选择项目进入工作台" actions={openWizard} />
      {body}
      <ProjectWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onRegistered={() => list.refetch()}
      />
    </>
  )
}
