import { useId } from 'react'
import { NavLink } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useAttention, useHerdrStatus } from '../api/hooks'
import { isRemoteWorkspace, needsActionCount } from '../api/normalize'
import { useProjectRegistryList } from '../api/registry'
import { routes } from '../app/routes'
import { GLOBAL_SCOPE, projectScope, useCapability } from '../state/capabilities'
import { useSelection } from '../state/selection'

function RailLink({
  to,
  icon,
  label,
  end = false,
  badge,
  mobileCore = false,
  mobileHidden = false,
}: {
  to: string
  icon: string
  label: string
  end?: boolean
  badge?: number
  mobileCore?: boolean
  mobileHidden?: boolean
}) {
  const mobileClasses = `${mobileCore ? ' rail-item--mobile-core' : ''}${mobileHidden ? ' rail-item--mobile-hidden' : ''}`
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => `rail-item${isActive ? ' rail-item--active' : ''}${mobileClasses}`}
      title={label}
    >
      <span className="rail-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="rail-label ellipsis">{label}</span>
      {mobileCore ? <span className="rail-mobile-label">{label}</span> : null}
      {badge != null && badge > 0 ? <span className="rail-badge">{badge}</span> : null}
    </NavLink>
  )
}

/**
 * 可见但不可用的 rail 项：可聚焦（tabIndex=0 + aria-disabled），
 * reason 用 aria-describedby 关联 sr-only 节点（不只靠 title），Enter/Space/click 被拦截。
 */
function DisabledRailItem({
  icon,
  label,
  reason,
  mobileHidden = false,
}: {
  icon: string
  label: string
  reason: string
  mobileHidden?: boolean
}) {
  const descId = useId()
  return (
    <>
      <span
        className={`rail-item rail-item--disabled${mobileHidden ? ' rail-item--mobile-hidden' : ''}`}
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
  const available = q.data?.data.available

  let tone: RuntimeTone
  let line1: string
  let line2: string | null = null

  if (q.isPending) {
    tone = 'muted'
    line1 = 'Herdr 检查中…'
  } else if (q.isError) {
    tone = 'danger'
    line1 = 'Herdr degraded'
    line2 = q.error instanceof ApiError ? q.error.message : '状态查询失败'
  } else if (available === false) {
    tone = 'danger'
    line1 = 'Herdr degraded'
    line2 = '本地 Herdr 二进制不可用'
  } else {
    tone = 'success'
    line1 = 'Herdr'
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
  const registry = useProjectRegistryList()
  const scope = projectSlug ? projectScope(projectSlug) : GLOBAL_SCOPE
  const remoteHerdrCap = useCapability('remoteHerdr', scope)
  // 项目段导航：仅 capability available 才展示（unavailable 不做主导航）
  const recoveryCap = useCapability('recovery.review', scope)
  const activityCap = useCapability('activity.feed', scope)
  const memoryCap = useCapability('memory.local', scope)
  const inboxBadge = needsActionCount(attention.data?.data)
  const noProjects = registry.data?.data.items.length === 0

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
          <RailLink
            to={routes.overview()}
            icon="◉"
            label={noProjects ? '开始使用' : '需要你处理'}
            badge={noProjects ? undefined : inboxBadge}
          />
          <RailLink to={routes.projects()} icon="▦" label="项目" mobileCore />
          {noProjects ? null : (
            <RailLink to={routes.inbox()} icon="✉" label="提问与回复" badge={inboxBadge} />
          )}
          <RailLink to={routes.settings()} icon="⚙" label="设置" />
        </div>

        {projectSlug ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前项目</p>
            <p className="rail-context rail-label ellipsis" title={project?.name ?? projectSlug}>
              {project?.name ?? projectSlug}
              {project?.branch ? <span className="rail-branch"> · {project.branch}</span> : null}
            </p>
            <RailLink to={routes.project.workbench(projectSlug)} icon="▣" label="项目概览" mobileHidden />
            {recoveryCap.available ? (
              <RailLink to={routes.project.recovery(projectSlug)} icon="⛨" label="变更审核" mobileHidden />
            ) : null}
            {activityCap.available ? (
              <RailLink to={routes.project.activity(projectSlug)} icon="≣" label="动态" mobileHidden />
            ) : null}
            {memoryCap.available ? (
              <RailLink to={routes.project.memory(projectSlug)} icon="◈" label="项目记忆" mobileHidden />
            ) : null}
            {(project?.workspaces ?? []).map((w) => {
              if (!w.id) return null
              // Remote fail-closed：remoteHerdr 关闭时远程 workspace 可见但 disabled
              if (isRemoteWorkspace(w) && !remoteHerdrCap.available) {
                return (
                  <DisabledRailItem
                    key={w.id}
                    icon="☁"
                    label={w.name ?? w.id}
                    reason={remoteHerdrCap.reason ?? '远程控制暂未接通'}
                    mobileHidden
                  />
                )
              }
              return (
                <RailLink
                  key={w.id}
                  to={routes.workspace.home(projectSlug, w.id)}
                  icon={isRemoteWorkspace(w) ? '☁' : '▹'}
                  label={w.name ?? w.id}
                  mobileHidden
                />
              )
            })}
          </div>
        ) : null}

        {projectSlug && workspaceId ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前工作空间</p>
            <p className="rail-context rail-label ellipsis" title={workspace?.name ?? workspaceId}>
              {workspace?.name ?? workspaceId}
            </p>
            <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="▣" label="工作空间概览" mobileHidden />
            <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" mobileCore />
            <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" mobileCore />
          </div>
        ) : null}
      </div>

      <RuntimeMini />
    </nav>
  )
}
