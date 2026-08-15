import { useEffect, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { isRemoteWorkspace } from '../api/normalize'
import { useProjectRegistryList } from '../api/registry'
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

function localWorkspaces(
  workspaces: { id?: string; name?: string; location?: string; runtime?: string }[] | undefined,
) {
  return (workspaces ?? []).filter((item) => !isRemoteWorkspace(item) && item.id)
}

function workspaceLabel(name: string | undefined): string {
  return name && name !== '' ? name : '工作空间'
}

export function Rail() {
  const location = useLocation()
  const { projectSlug, workspaceId, project, workspace } = useSelection()
  const registry = useProjectRegistryList()
  const [treeOpen, setTreeOpen] = useState(false)

  useEffect(() => {
    setTreeOpen(false)
  }, [location.pathname])

  const projects = (registry.data?.data.items ?? []).filter((item) => item.lifecycle !== 'archived')
  const treeProjects = projectSlug
    ? projects.filter((item) => item.slug !== projectSlug)
    : projects
  const currentWorkspaces = localWorkspaces(project?.workspaces)
  const currentProjectTitle = project?.name && project.name !== '' ? project.name : projectSlug

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail-brand">
        <span className="rail-brand-mark" aria-hidden="true">C</span>
        <span className="rail-label rail-brand-text">Cockpit <span className="rail-version">2.0</span></span>
      </div>

      <div className="rail-scroll">
        <div className="rail-section">
          <button
            type="button"
            className={`rail-item rail-item--mobile-core${treeOpen ? ' rail-item--active' : ''}`}
            title="项目"
            aria-expanded={treeOpen}
            aria-controls="rail-project-tree"
            onClick={() => setTreeOpen((open) => !open)}
          >
            <span className="rail-icon" aria-hidden="true">▦</span>
            <span className="rail-label ellipsis">项目</span>
            <span className="rail-mobile-label">项目</span>
          </button>
          {treeOpen ? (
            <button
              type="button"
              className="rail-tree-backdrop"
              aria-label="关闭项目列表"
              onClick={() => setTreeOpen(false)}
            />
          ) : null}
          <div
            id="rail-project-tree"
            className={`rail-tree${treeOpen ? ' rail-tree--open' : ''}${projectSlug ? ' rail-tree--context' : ''}`}
          >
            {treeOpen || projectSlug ? (
              <>
                {treeProjects.map((item) => (
                  <RailLink
                    key={item.project_id}
                    to={routes.project.workbench(item.slug)}
                    icon="▣"
                    label={item.display_name}
                    mobileHidden
                  />
                ))}
                <RailLink to={routes.projects()} icon="☰" label="管理项目" mobileHidden />
                <RailLink to={routes.projects({ wizard: true })} icon="+" label="添加项目" mobileHidden />
              </>
            ) : null}
          </div>
        </div>

        {projectSlug ? (
          <div className="rail-section">
            <p className="rail-heading rail-label">当前项目</p>
            <p className="rail-context rail-label ellipsis" title={currentProjectTitle ?? undefined}>
              {currentProjectTitle}
            </p>
            {currentWorkspaces.map((item) => (
              <RailLink
                key={item.id}
                to={routes.workspace.home(projectSlug, item.id!)}
                icon="▹"
                label={workspaceLabel(item.name)}
                mobileHidden
              />
            ))}
            {workspaceId ? (
              <div className="rail-section rail-section--nested">
                <p className="rail-heading rail-label">当前工作空间</p>
                {currentWorkspaces.some((item) => item.id === workspaceId) ? null : (
                  <p className="rail-context rail-label ellipsis" title={workspaceLabel(workspace?.name)}>
                    {workspaceLabel(workspace?.name)}
                  </p>
                )}
                <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="◉" label="工作对话" mobileCore />
                <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" mobileCore />
                <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" mobileCore />
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </nav>
  )
}
