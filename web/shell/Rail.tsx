import { useEffect, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { isRemoteWorkspace } from '../api/normalize'
import { useProjectRegistryList, type RegistryProject } from '../api/registry'
import { routes } from '../app/routes'
import { useSelection } from '../state/selection'

function RailLink({
  to,
  icon,
  label,
  end = false,
  mobileCore = false,
  mobileHidden = false,
  nested = false,
  titled = true,
}: {
  to: string
  icon: string
  label: string
  end?: boolean
  mobileCore?: boolean
  mobileHidden?: boolean
  nested?: boolean
  titled?: boolean
}) {
  const extra = `${mobileCore ? ' rail-item--mobile-core' : ''}${mobileHidden ? ' rail-item--mobile-hidden' : ''}${nested ? ' rail-item--nested' : ''}`
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => `rail-item${isActive ? ' rail-item--active' : ''}${extra}`}
      title={titled ? label : undefined}
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

function WorkspaceFunctions({
  projectSlug,
  workspaceId,
  mobile,
}: {
  projectSlug: string
  workspaceId: string
  mobile: boolean
}) {
  if (mobile) {
    return (
      <>
        <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="◉" label="工作对话" mobileCore />
        <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" mobileCore />
        <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" mobileCore />
      </>
    )
  }
  return (
    <>
      <RailLink to={routes.workspace.home(projectSlug, workspaceId)} end icon="◉" label="工作" mobileHidden nested titled={false} />
      <RailLink to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" mobileHidden nested titled={false} />
      <RailLink to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" mobileHidden nested titled={false} />
    </>
  )
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
  const currentWorkspaces = localWorkspaces(project?.workspaces)
  const currentProjectTitle = project?.name && project.name !== '' ? project.name : projectSlug
  const inCurrent = (item: RegistryProject) => item.slug === projectSlug
  const treeProjects = projectSlug && currentProjectTitle && !projects.some(inCurrent)
    ? [
        {
          project_id: project?.project_id ?? projectSlug,
          slug: projectSlug,
          display_name: currentProjectTitle,
        } as RegistryProject,
        ...projects,
      ]
    : projects
  const showTree = treeOpen || Boolean(projectSlug)

  return (
    <nav className="rail" aria-label="主导航">
      <div className="rail-brand">
        <span className="rail-brand-mark" aria-hidden="true">C</span>
        <span className="rail-label rail-brand-text">Cockpit <span className="rail-version">2.0</span></span>
      </div>

      <div className="rail-scroll">
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
          {showTree ? (
            <>
              {treeProjects.map((item) => {
                const current = item.slug === projectSlug
                return (
                  <div key={item.project_id} className="rail-tree-group">
                    {current ? (
                      <>
                        <p className="rail-heading rail-label">当前项目</p>
                        <p className="rail-context rail-label ellipsis" title={item.display_name}>
                          {item.display_name}
                        </p>
                        {currentWorkspaces.map((ws) => (
                          <div key={ws.id} className="rail-tree-workspace">
                            <RailLink
                              to={routes.workspace.home(projectSlug!, ws.id!)}
                              icon="▹"
                              label={workspaceLabel(ws.name)}
                              mobileHidden
                            />
                            {ws.id === workspaceId ? (
                              <WorkspaceFunctions
                                projectSlug={projectSlug!}
                                workspaceId={workspaceId}
                                mobile={false}
                              />
                            ) : null}
                          </div>
                        ))}
                        {workspaceId && !currentWorkspaces.some((ws) => ws.id === workspaceId) ? (
                          <div className="rail-tree-workspace">
                            <p className="rail-context rail-label ellipsis" title={workspaceLabel(workspace?.name)}>
                              {workspaceLabel(workspace?.name)}
                            </p>
                            <WorkspaceFunctions
                              projectSlug={projectSlug}
                              workspaceId={workspaceId}
                              mobile={false}
                            />
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <RailLink
                        to={routes.project.workbench(item.slug)}
                        icon="▣"
                        label={item.display_name}
                        mobileHidden
                      />
                    )}
                  </div>
                )
              })}
              <RailLink to={routes.projects()} icon="☰" label="管理项目" mobileHidden />
              <RailLink to={routes.projects({ wizard: true })} icon="+" label="添加项目" mobileHidden />
            </>
          ) : null}
        </div>

        {projectSlug && workspaceId ? (
          <div className="rail-section rail-mobile-nav">
            <p className="rail-heading rail-label">当前工作空间</p>
            <WorkspaceFunctions projectSlug={projectSlug} workspaceId={workspaceId} mobile />
          </div>
        ) : null}
      </div>
    </nav>
  )
}
