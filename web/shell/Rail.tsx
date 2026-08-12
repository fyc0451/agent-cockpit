import { useId } from 'react'
import { NavLink } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useAttention, useHerdrStatus } from '../api/hooks'
import { isRemoteWorkspace, needsActionCount } from '../api/normalize'
import { routes } from '../app/routes'
import { GLOBAL_SCOPE, projectScope, useCapability } from '../state/capabilities'
import { useSelection } from '../state/selection'

function RailLink({
  to,
  icon,
  label,
  end = false,
  badge,
}: {
  to: string
  icon: string
  label: string
  end?: boolean
  badge?: number
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => `rail-item${isActive ? ' rail-item--active' : ''}`}
      title={label}
    >
      <span className="rail-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="rail-label ellipsis">{label}</span>
      {badge != null && badge > 0 ? <span className="rail-badge">{badge}</span> : null}
    </NavLink>
  )
}

/**
 * 可见但不可用的 rail 项：可聚焦（tabIndex=0 + aria-disabled），
 * reason 用 aria-describedby 关联 sr-only 节点（不只靠 title），Enter/Space/click 被拦截。
 */
function DisabledRailItem({ icon, label, reason }: { icon: string; label: string; reason: string }) {
  const descId = useId()
  return (
    <>
      <span
        className="rail-item rail-item--disabled"
        title={reason}
        role="link"
        aria-disabled="true"
        aria-describedby={descId}
        tabIndex={0}
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            e.stopPropagation()
          }
        }}
      >
        <span className="rail-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="rail-label ellipsis">{label}</span>
      </span>
      <span id={descId} className="sr-only">
        {label}不可用：{reason}
      </span>
    </>
  )
}

type RuntimeTone = 'muted' | 'success' | 'warning' | 'danger'

/**
 * RuntimeMini：loading / healthy=false / source disconnected / source stale
 * 都不得显示绿色 healthy；success 只在数据健康且来源 available 时。
 */
function RuntimeMini() {
  const q = useHerdrStatus()
  const name = q.data?.data.name ?? 'Herdr'
  const herdrSource = q.data?.meta?.sources?.find((s) => s.name === 'herdr')

  let tone: RuntimeTone
  let line1: string
  let line2: string | null = null

  if (q.isPending) {
    tone = 'muted'
    line1 = `${name} 检查中…`
  } else if (q.isError) {
    tone = 'danger'
    line1 = `${name} degraded`
    line2 = q.error instanceof ApiError ? q.error.message : '状态查询失败'
  } else if (herdrSource && herdrSource.status === 'stale') {
    tone = 'warning'
    line1 = `${name} 数据可能不是最新`
    line2 = herdrSource.observed_at ? `上次更新：${herdrSource.observed_at}` : null
  } else if (herdrSource && herdrSource.status != null && herdrSource.status !== 'available') {
    tone = 'danger'
    line1 = `${name} degraded`
    line2 = herdrSource.reason ?? herdrSource.status ?? null
  } else if (q.data?.data.healthy === false) {
    tone = 'danger'
    line1 = `${name} 异常`
    line2 = q.data.data.message ?? null
  } else {
    tone = 'success'
    line1 = name
  }

  return (
    <div className="runtime-mini" data-testid="runtime-mini" data-tone={tone}>
      <span className={`runtime-dot runtime-dot--${tone}`} aria-hidden="true" />
      <span className="rail-label runtime-mini-text">
        <span className="ellipsis">{line1}</span>
        {line2 ? <span className="ellipsis runtime-mini-reason">{line2}</span> : null}
      </span>
    </div>
  )
}

export function Rail() {
  const { projectSlug, workspaceId, project, workspace } = useSelection()
  const attention = useAttention()
  const scope = projectSlug ? projectScope(projectSlug) : GLOBAL_SCOPE
  const automationCap = useCapability('automation', scope)
  const remoteHerdrCap = useCapability('remoteHerdr', scope)
  const inboxBadge = needsActionCount(attention.data?.data)

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail-brand">
        <span className="rail-brand-mark" aria-hidden="true">
          C
        </span>
        <span className="rail-label rail-brand-text">
          Cockpit <span className="rail-version">2.0</span>
        </span>
      </div>

      <div className="rail-scroll">
        <div className="rail-section">
          <RailLink to={routes.overview()} icon="◉" label="需要你处理" badge={inboxBadge} />
          <RailLink to={routes.projects()} icon="▦" label="项目" />
          <RailLink to={routes.inbox()} icon="✉" label="提问与回复" badge={inboxBadge} />
          <RailLink to={routes.settings()} icon="⚙" label="设置" />
        </div>

        {projectSlug ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前项目</p>
            <p className="rail-context rail-label ellipsis" title={project?.name ?? projectSlug}>
              {project?.name ?? projectSlug}
              {project?.branch ? <span className="rail-branch"> · {project.branch}</span> : null}
            </p>
            <RailLink to={routes.project.workbench(projectSlug)} icon="▣" label="工作台" />
            <RailLink to={routes.project.recovery(projectSlug)} icon="⛨" label="变更审核" />
            <RailLink to={routes.project.activity(projectSlug)} icon="≣" label="动态" />
            <RailLink to={routes.project.memory(projectSlug)} icon="◈" label="项目记忆" />
            {(project?.workspaces ?? []).map((w) => {
              if (!w.id) return null
              // Remote fail-closed：remoteHerdr 关闭时远程 workspace 可见但 disabled
              if (isRemoteWorkspace(w) && !remoteHerdrCap.available) {
                return (
                  <DisabledRailItem
                    key={w.id}
                    icon="☁"
                    label={w.name ?? w.id}
                    reason={remoteHerdrCap.reason ?? '远程 Herdr 控制未接通'}
                  />
                )
              }
              return (
                <RailLink
                  key={w.id}
                  to={routes.workspace.home(projectSlug, w.id)}
                  icon={isRemoteWorkspace(w) ? '☁' : '▹'}
                  label={w.name ?? w.id}
                />
              )
            })}
          </div>
        ) : null}

        {projectSlug && workspaceId ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前 Workspace</p>
            <p className="rail-context rail-label ellipsis" title={workspace?.name ?? workspaceId}>
              {workspace?.name ?? workspaceId}
            </p>
            <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="▣" label="工作台" />
            <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" />
            <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" />
            <DisabledRailItem icon="✉" label="通信" reason={automationCap.reason ?? '未接通'} />
            <RailLink to={routes.workspace.tasks(projectSlug, workspaceId)} icon="☑" label="任务" />
            <RailLink to={routes.workspace.editor(projectSlug, workspaceId)} icon="✎" label="编辑器" />
            <RailLink to={routes.workspace.browser(projectSlug, workspaceId)} icon="◎" label="浏览器" />
            <DisabledRailItem icon="⛁" label="连接" reason={remoteHerdrCap.reason ?? '未接通'} />
          </div>
        ) : null}
      </div>

      <RuntimeMini />
    </nav>
  )
}
