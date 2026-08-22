import { generatePath } from 'react-router-dom'
import {
  NAV_ROUTES,
  PROJECT_PARAM,
  routePatterns,
  routes,
  WORKSPACE_PARAM,
} from '../app/routes'
import { parseSelection } from '../state/selection'

describe('routes 单一权威模块', () => {
  it('静态路由 builders', () => {
    expect(routes.overview()).toBe('/overview')
    expect(routes.welcome()).toBe('/welcome')
    expect(routes.projects()).toBe('/projects')
  })

  it('builder 对 slug / workspace id 做 URL 编码（含特殊字符）', () => {
    const slug = 'p 1/中?#'
    expect(routes.project.workbench(slug)).toBe(
      `/projects/${encodeURIComponent(slug)}/workbench`,
    )
    expect(routes.project.workbench(slug)).not.toContain('/中/')
    const wid = 'w 1/#'
    expect(routes.workspace.files('p1', wid)).toBe(
      `/projects/p1/workspaces/${encodeURIComponent(wid)}/files`,
    )
  })

  it('query 参数由 builder 拼接', () => {
    expect(routes.settings({ view: 'doctor' })).toBe('/settings?view=doctor')
    expect(routes.settings({ view: 'upgrade' })).toBe('/settings?view=upgrade')
    expect(routes.settings({ view: 'team' })).toBe('/settings?view=team')
    expect(routes.settings({ view: 'appearance' })).toBe('/settings')
    expect(routes.settings()).toBe('/settings')
    expect(routes.inbox({ view: 'needs-action' })).toBe('/inbox?view=needs-action')
    expect(routes.inbox()).toBe('/inbox')
  })

  it('黄金路径 URL 合同：projects?wizard=1 与 workbench?createWorkspace=1', () => {
    expect(routes.projects()).toBe('/projects')
    expect(routes.projects({ wizard: true })).toBe('/projects?wizard=1')
    expect(routes.project.workbench('p1')).toBe('/projects/p1/workbench')
    expect(routes.project.workbench('p1', { createWorkspace: true })).toBe(
      '/projects/p1/workbench?createWorkspace=1',
    )
  })

  it('routePatterns ↔ builders 一致：generatePath 输出与 builder 相同', () => {
    const pairs: [pattern: string, built: string, params: Record<string, string>][] = [
      [routePatterns.projectWorkbench, routes.project.workbench('p1'), { [PROJECT_PARAM]: 'p1' }],
      [routePatterns.projectMemory, routes.project.memory('p1'), { [PROJECT_PARAM]: 'p1' }],
      [routePatterns.projectRecovery, routes.project.recovery('p1'), { [PROJECT_PARAM]: 'p1' }],
      [routePatterns.projectActivity, routes.project.activity('p1'), { [PROJECT_PARAM]: 'p1' }],
      [
        routePatterns.workspaceBase,
        routes.workspace.home('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceFiles,
        routes.workspace.files('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceTerminal,
        routes.workspace.terminal('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceAgent,
        routes.workspace.agent('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceTasks,
        routes.workspace.tasks('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceGit,
        routes.workspace.git('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceEditor,
        routes.workspace.editor('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceBrowser,
        routes.workspace.browser('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
      [
        routePatterns.workspaceActivity,
        routes.workspace.activity('p1', 'w1'),
        { [PROJECT_PARAM]: 'p1', [WORKSPACE_PARAM]: 'w1' },
      ],
    ]
    for (const [pattern, built, params] of pairs) {
      expect(generatePath(pattern, params)).toBe(built)
    }
  })

  it('builder 输出可被 parseSelection 还原参数（round-trip）', () => {
    // 注：slug 不含 '/'——含斜杠的标识符无法在任何 hash 路由形态下 round-trip，
    // 属于 URL 身份裁决（slug vs project_id）需要规避的输入
    const slug = 'p 1中'
    const wid = 'w 1'
    expect(parseSelection(routes.workspace.terminal(slug, wid))).toEqual({
      projectSlug: slug,
      workspaceId: wid,
    })
    expect(parseSelection(routes.project.workbench(slug))).toEqual({
      projectSlug: slug,
      workspaceId: null,
    })
    expect(parseSelection(routes.overview())).toEqual({ projectSlug: null, workspaceId: null })
  })

  it('NAV_ROUTES 清单全部指向合法静态路由', () => {
    expect(NAV_ROUTES.length).toBeGreaterThan(0)
    for (const t of NAV_ROUTES) {
      const path = t.to()
      const [pathname] = path.split('?')
      const known = [
        routePatterns.overview,
        routePatterns.projects,
        routePatterns.inbox,
        routePatterns.settings,
        routePatterns.welcome,
        routePatterns.team,
      ]
      expect(known).toContain(pathname)
    }
    // doctor 深链保持冻结形态
    const doctor = NAV_ROUTES.find((t) => t.name.includes('Doctor'))
    expect(doctor?.to()).toBe('/settings?view=doctor')
    expect(NAV_ROUTES.some((t) => t.name === '欢迎页')).toBe(false)
  })
})
