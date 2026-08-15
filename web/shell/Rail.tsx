import { NavLink } from 'react-router-dom'
import { isRemoteWorkspace } from '../api/normalize'
import { routes } from '../app/routes'
import { useSelection } from '../state/selection'

function RailLink({
  to,
  icon,
  label,
  end = false,
  mobileCore = false,
  mobileHidden = false,
}: {
  to: string
  icon: string
  label: string
  end?: boolean
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
      <span className="rail-icon" aria-hidden="true">{icon}</span>
      <span className="rail-label ellipsis">{label}</span>
      {mobileCore ? <span className="rail-mobile-label">{label}</span> : null}
    </NavLink>
  )
}

export function Rail() {
  const { projectSlug, workspaceId, project, workspace } = useSelection()

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail-brand">
        <span className="rail-brand-mark" aria-hidden="true">C</span>
        <span className="rail-label rail-brand-text">Cockpit <span className="rail-version">2.0</span></span>
      </div>

      <div className="rail-scroll">
        <div className="rail-section">
          <RailLink to={routes.projects()} icon="▦" label="项目" mobileCore />
        </div>

        {projectSlug ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前项目</p>
            <p className="rail-context rail-label ellipsis" title={project?.name ?? projectSlug}>
              {project?.name ?? projectSlug}
            </p>
            {(project?.workspaces ?? [])
              .filter((item) => !isRemoteWorkspace(item))
              .map((item) => item.id ? (
                <RailLink
                  key={item.id}
                  to={routes.workspace.home(projectSlug, item.id)}
                  icon="▹"
                  label={item.name ?? item.id}
                  mobileHidden
                />
              ) : null)}
          </div>
        ) : null}

        {projectSlug && workspaceId ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前工作空间</p>
            <p className="rail-context rail-label ellipsis" title={workspace?.name ?? workspaceId}>
              {workspace?.name ?? workspaceId}
            </p>
            <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="◉" label="工作对话" mobileCore />
            <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" mobileCore />
            <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" mobileCore />
          </div>
        ) : null}
      </div>
    </nav>
  )
}
