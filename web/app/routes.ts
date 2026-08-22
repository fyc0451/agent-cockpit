/**
 * 路由单一权威模块（G1 冻结深链合同：path segment 放 ID，query 放筛选/子视图）。
 *
 * URL 身份单点：Project 在 URL 中的身份（slug vs project_id）正由 lead/Luna 最终裁决，
 * workspace 参数同理。裁决落地后只允许修改本文件（参数名 / pattern / builder 实现），
 * 业务代码一律通过 routePatterns 与 routes builders 消费，不得写字面量路径。
 * encodeURIComponent 只允许出现在本文件内部（API client 的 /api endpoint 拼接除外）。
 */

export const PROJECT_PARAM = 'projectSlug'
export const WORKSPACE_PARAM = 'workspaceId'

const PROJECT_BASE_PATTERN = `/projects/:${PROJECT_PARAM}`
const WORKSPACE_BASE_PATTERN = `${PROJECT_BASE_PATTERN}/workspaces/:${WORKSPACE_PARAM}`

/** 所有 <Route path> 的模式字符串；App.tsx 与 selection 解析只消费这里 */
export const routePatterns = {
  chat: '/chat',
  overview: '/overview',
  welcome: '/welcome',
  projects: '/projects',
  inbox: '/inbox',
  settings: '/settings',
  team: '/team',
  projectBase: PROJECT_BASE_PATTERN,
  projectWorkbench: `${PROJECT_BASE_PATTERN}/workbench`,
  projectMemory: `${PROJECT_BASE_PATTERN}/memory`,
  projectRecovery: `${PROJECT_BASE_PATTERN}/recovery`,
  projectActivity: `${PROJECT_BASE_PATTERN}/activity`,
  workspaceBase: WORKSPACE_BASE_PATTERN,
  workspaceActivity: `${WORKSPACE_BASE_PATTERN}/activity`,
  workspaceFiles: `${WORKSPACE_BASE_PATTERN}/files`,
  workspaceTerminal: `${WORKSPACE_BASE_PATTERN}/terminal`,
  workspaceAgent: `${WORKSPACE_BASE_PATTERN}/agent`,
  workspaceTasks: `${WORKSPACE_BASE_PATTERN}/tasks`,
  workspaceGit: `${WORKSPACE_BASE_PATTERN}/git`,
  workspaceEditor: `${WORKSPACE_BASE_PATTERN}/editor`,
  workspaceBrowser: `${WORKSPACE_BASE_PATTERN}/browser`,
} as const

export type InboxView = 'needs-action'
export type SettingsView = 'doctor' | 'appearance' | 'upgrade' | 'team'

function seg(id: string): string {
  return encodeURIComponent(id)
}

function projectBasePath(project: string): string {
  return `/projects/${seg(project)}`
}

function workspaceBasePath(project: string, workspace: string): string {
  return `${projectBasePath(project)}/workspaces/${seg(workspace)}`
}

function withQuery(path: string, params: Record<string, string | undefined>): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v) qs.set(k, v)
  }
  const s = qs.toString()
  return s ? `${path}?${s}` : path
}

/** Typed builders：query 参数由 builder 拼接，调用方只传 typed 选项 */
export const routes = {
  chat: (opts: { session?: string } = {}): string =>
    withQuery(routePatterns.chat, { session: opts.session }),
  teamInvite: (inviteCode: string, projectSlug: string): string =>
    withQuery(routePatterns.chat, {
      team_invite: inviteCode,
      team_project: projectSlug,
    }),
  overview: (): string => routePatterns.overview,
  welcome: (): string => routePatterns.welcome,
  projects: (opts: { wizard?: boolean } = {}): string =>
    withQuery(routePatterns.projects, { wizard: opts.wizard ? '1' : undefined }),
  inbox: (opts: { view?: InboxView } = {}): string =>
    withQuery(routePatterns.inbox, { view: opts.view }),
  settings: (opts: { view?: SettingsView } = {}): string =>
    withQuery(routePatterns.settings, {
      view: opts.view && opts.view !== 'appearance' ? opts.view : undefined,
    }),
  team: (): string => routePatterns.team,
  project: {
    workbench: (project: string, opts: { createWorkspace?: boolean } = {}): string =>
      withQuery(`${projectBasePath(project)}/workbench`, {
        createWorkspace: opts.createWorkspace ? '1' : undefined,
      }),
    memory: (project: string): string => `${projectBasePath(project)}/memory`,
    recovery: (project: string): string => `${projectBasePath(project)}/recovery`,
    activity: (project: string): string => `${projectBasePath(project)}/activity`,
  },
  workspace: {
    home: (project: string, workspace: string): string => workspaceBasePath(project, workspace),
    activity: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/activity`,
    files: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/files`,
    terminal: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/terminal`,
    agent: (project: string, workspace: string, opts: { agentId?: string } = {}): string =>
      withQuery(`${workspaceBasePath(project, workspace)}/agent`, { agent: opts.agentId }),
    tasks: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/tasks`,
    git: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/git`,
    editor: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/editor`,
    browser: (project: string, workspace: string): string => `${workspaceBasePath(project, workspace)}/browser`,
  },
}

/** '#' 前缀的 href 形态（StatusState docsRoute 等 <a href> 消费），由 builder 派生 */
export const routeHrefs = {
  doctor: (): string => `#${routes.settings({ view: 'doctor' })}`,
}

/** 命令面板「快速前往」等静态导航清单：label + builder，统一从这里派生 */
export interface NavRouteMeta {
  name: string
  keywords?: string
  to: () => string
}

export const NAV_ROUTES: readonly NavRouteMeta[] = [
  { name: 'Overview · 需要你处理', keywords: 'overview attention', to: routes.overview },
  { name: '项目列表', keywords: 'projects switch', to: routes.projects },
  { name: 'Inbox · 提问与回复', keywords: 'inbox questions', to: () => routes.inbox() },
  { name: '设置', keywords: 'settings preferences', to: () => routes.settings() },
  { name: '团队管理', keywords: 'team admin members invite approvals', to: routes.team },
  {
    name: '环境自检 Doctor',
    keywords: 'doctor env check diagnostics',
    to: () => routes.settings({ view: 'doctor' }),
  },
]
