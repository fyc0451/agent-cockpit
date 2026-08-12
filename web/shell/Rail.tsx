import { NavLink } from 'react-router-dom'
import { useAttention, useHerdrStatus } from '../api/hooks'
import { needsActionCount } from '../api/normalize'
import { routes } from '../app/routes'
import { useCapability } from '../state/capabilities'
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

function DisabledRailItem({ icon, label, reason }: { icon: string; label: string; reason: string }) {
  return (
    <span className="rail-item rail-item--disabled" title={reason} aria-disabled="true">
      <span className="rail-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="rail-label ellipsis">{label}</span>
    </span>
  )
}

function RuntimeMini() {
  const q = useHerdrStatus()
  const degraded = q.isError
  const name = q.data?.data.name ?? 'Herdr'
  return (
    <div className="runtime-mini" data-testid="runtime-mini">
      <span
        className={`runtime-dot ${degraded ? 'runtime-dot--danger' : 'runtime-dot--success'}`}
        aria-hidden="true"
      />
      <span className="rail-label ellipsis">{degraded ? `${name} degraded` : name}</span>
    </div>
  )
}

export function Rail() {
  const { projectSlug, workspaceId, project, workspace } = useSelection()
  const attention = useAttention()
  const automationCap = useCapability('automation')
  const remoteHerdrCap = useCapability('remoteHerdr')
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
            {(project?.workspaces ?? []).map((w) =>
              w.id ? (
                <RailLink
                  key={w.id}
                  to={routes.workspace.home(projectSlug, w.id)}
                  icon={w.location === 'remote' ? '☁' : '▹'}
                  label={w.name ?? w.id}
                />
              ) : null,
            )}
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
