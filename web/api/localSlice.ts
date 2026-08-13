// SLICE-001 Local 只读纵切的端点与类型唯一硬编码点。
// 冻结口径：docs/contracts/cockpit2-local-slice-v1.md（backend accepted 74177fd）
// + docs/contracts/cockpit2-r0-local-only.md：
// - 五条新 GET 路由严格 G3 envelope；Workspace public DTO 精确 12 键（含嵌套
//   repo_location{node_id,availability}），绝不含 canonical_path/cwd/human_key。
// - tree={path,entries[{name,type,size,ext}]}（entry 无 path，页面按当前 path+name
//   组装相对路径）；content={path,size,binary,text?}（binary=true 无 text）；
//   search={path,query,results[{name,path,type,size,ext}],truncated}。
// - workspace detail meta.capabilities 是 files.read / terminal.pty 权威值
//   （terminal.pty 恒 false=workspace_terminal_ticket_deferred）。
// - legacy env-check 裸 {herdr,agents,agent_mail}；legacy workbench 裸四键
//   {project,assignments,sessions,source}。legacyGet 只服务这两条明确路由，
//   通用 api/client.ts 不放宽。
// 纪律：envelope、必填字段与键集绝不宽容（ProtocolError fail-closed）。

import { useQuery } from '@tanstack/react-query'
import {
  GLOBAL_SCOPE,
  projectScope,
  useReportCapabilities,
  workspaceScope,
  type CapabilityScope,
} from '../state/capabilities'
import { ApiError, ProtocolError, apiGet } from './client'

export const LOCAL_SLICE_API = {
  workspaces: (projectId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces`,
  workspace: (projectId: string, workspaceId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}`,
  workspaceFiles: (projectId: string, workspaceId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/files`,
  workspaceFileContent: (projectId: string, workspaceId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/files/content`,
  workspaceFileSearch: (projectId: string, workspaceId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/files/search`,
  legacyWorkbench: (slug: string) => `/api/projects/${encodeURIComponent(slug)}/workbench`,
  legacyEnvCheck: '/api/env-check',
} as const

// ---------- Workspace（G3，精确 12 键 public DTO） ----------

export type RepoAvailability = 'available' | 'offline' | 'missing' | 'unknown'

export interface WorkspaceRepoLocationRef {
  node_id: string
  availability: RepoAvailability
}

export interface WorkspaceSummary {
  workspace_id: string // persisted ws_* 身份（URL 权威）
  project_id: string
  repo_location_id: string
  name: string
  goal: string | null
  isolation_kind: 'shared' | 'isolated_worktree' | 'review_detached'
  lifecycle: 'active' | 'archived'
  active_run_id: string | null
  version: number
  created_at: string
  updated_at: string
  repo_location: WorkspaceRepoLocationRef
}

/** list data 精确 {items}（无 next_cursor） */
export interface WorkspaceListData {
  items: WorkspaceSummary[]
}

/** Local/Remote 派生：repo_location.node_id === 'local' → local，其余 remote */
export function workspaceLocation(w: WorkspaceSummary): 'local' | 'remote' {
  return w.repo_location.node_id === 'local' ? 'local' : 'remote'
}

// ---------- Files（G3；只含相对 path） ----------

/** tree entry：{name,type,size,ext} 精确四键；无 path/kind */
export interface WorkspaceFileEntry {
  name: string
  type: 'dir' | 'file'
  size: number // 非负整数
  ext: string
}

export interface WorkspaceFilesData {
  path: string // 相对路径回声（root 为空串）
  entries: WorkspaceFileEntry[]
}

export interface WorkspaceFileContentData {
  path: string
  size: number // 非负整数
  binary: boolean
  text?: string // binary=false 时必填；binary=true 时无 text
}

export interface WorkspaceFileSearchResult {
  name: string
  path: string // 相对 path
  type: 'dir' | 'file'
  size: number
  ext: string
}

export interface WorkspaceFileSearchData {
  path: string
  query: string
  results: WorkspaceFileSearchResult[]
  truncated: boolean
}

// ---------- legacy 裸形状（严格键集 fail-closed） ----------

export interface LegacyWorkbenchProject {
  id: number
  slug: string
  created_at: string | number | null
}

export interface LegacyAssignment {
  assignment_id?: unknown
  assignment?: unknown
  assignee?: unknown
  expected_reply?: unknown
  deadline?: unknown
  status?: unknown
  closed_at?: unknown
  version?: unknown
  created_at?: unknown
  updated_at?: unknown
}

export interface LegacyPane {
  pane_id?: unknown
  agent?: unknown
  agent_status?: unknown
  focused?: unknown
  revision?: unknown
}

export interface LegacySession {
  session: string
  status?: unknown
  focused_pane_id?: unknown
  panes: LegacyPane[]
}

export interface LegacyWorkbench {
  project: LegacyWorkbenchProject
  assignments: LegacyAssignment[]
  sessions: LegacySession[]
  source: {
    available: boolean
    degraded: boolean
    observed_at: string | number | null
  }
}

export interface LegacyEnvCheckItem {
  installed: boolean
  path: string
}

export interface LegacyEnvCheck {
  herdr: LegacyEnvCheckItem
  agents: Record<string, LegacyEnvCheckItem>
  agent_mail: {
    available: boolean
    reason?: string | null
    read_available?: boolean
    write_available?: boolean
    write_reason?: string | null
  }
}

// ---------- 守卫原语（与 registry.ts 同款 fail-closed） ----------

function fail(field: string): never {
  throw new ProtocolError(`local-slice 响应必填字段缺失或类型错误：${field}`)
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function reqObj(v: unknown, field: string): Record<string, unknown> {
  if (!isObj(v)) fail(field)
  return v
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

function reqStringAllowEmpty(v: unknown, field: string): string {
  if (typeof v !== 'string') fail(field)
  return v
}

function reqBool(v: unknown, field: string): boolean {
  if (typeof v !== 'boolean') fail(field)
  return v
}

function optBool(v: unknown, field: string): boolean | undefined {
  if (v === undefined) return undefined
  return reqBool(v, field)
}

/** 非负整数（type 精确 int，bool 不算） */
function reqNonNegativeInt(v: unknown, field: string): number {
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 0) fail(field)
  return v
}

function reqNullableString(v: unknown, field: string): string | null {
  if (v === null) return null
  if (typeof v !== 'string') fail(field)
  return v
}

function reqEnum<T extends string>(v: unknown, field: string, allowed: readonly T[]): T {
  if (typeof v !== 'string' || !allowed.includes(v as T)) fail(field)
  return v as T
}

/** 严格键集：多键少键都 ProtocolError（F3 同款纪律） */
function reqExactKeys(o: Record<string, unknown>, keys: readonly string[], ctx: string): void {
  const actual = Object.keys(o).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((k, i) => k !== expected[i])) {
    fail(`${ctx} 键集`)
  }
}

// ---------- Workspace 守卫 ----------

const ISOLATION_KINDS = ['shared', 'isolated_worktree', 'review_detached'] as const
const LIFECYCLES = ['active', 'archived'] as const
const AVAILABILITIES = ['available', 'offline', 'missing', 'unknown'] as const

const WORKSPACE_KEYS = [
  'workspace_id',
  'project_id',
  'repo_location_id',
  'name',
  'goal',
  'isolation_kind',
  'lifecycle',
  'active_run_id',
  'version',
  'created_at',
  'updated_at',
  'repo_location',
] as const

export function assertWorkspaceSummary(raw: unknown, ctx = 'workspace'): WorkspaceSummary {
  const o = reqObj(raw, ctx)
  reqExactKeys(o, WORKSPACE_KEYS, ctx)
  const loc = reqObj(o.repo_location, `${ctx}.repo_location`)
  reqExactKeys(loc, ['node_id', 'availability'], `${ctx}.repo_location`)
  const version = o.version
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    fail(`${ctx}.version`)
  }
  return {
    workspace_id: reqString(o.workspace_id, `${ctx}.workspace_id`),
    project_id: reqString(o.project_id, `${ctx}.project_id`),
    repo_location_id: reqString(o.repo_location_id, `${ctx}.repo_location_id`),
    name: reqString(o.name, `${ctx}.name`),
    goal: reqNullableString(o.goal, `${ctx}.goal`),
    isolation_kind: reqEnum(o.isolation_kind, `${ctx}.isolation_kind`, ISOLATION_KINDS),
    lifecycle: reqEnum(o.lifecycle, `${ctx}.lifecycle`, LIFECYCLES),
    active_run_id: reqNullableString(o.active_run_id, `${ctx}.active_run_id`),
    version,
    created_at: reqString(o.created_at, `${ctx}.created_at`),
    updated_at: reqString(o.updated_at, `${ctx}.updated_at`),
    repo_location: {
      node_id: reqString(loc.node_id, `${ctx}.repo_location.node_id`),
      availability: reqEnum(loc.availability, `${ctx}.repo_location.availability`, AVAILABILITIES),
    },
  }
}

export function assertWorkspaceListData(raw: unknown): WorkspaceListData {
  const o = reqObj(raw, 'workspaces')
  reqExactKeys(o, ['items'], 'workspaces')
  if (!Array.isArray(o.items)) fail('workspaces.items')
  return {
    items: (o.items as unknown[]).map((item, i) => assertWorkspaceSummary(item, `workspaces.items[${i}]`)),
  }
}

// ---------- Files 守卫 ----------

const FILE_TYPES = ['dir', 'file'] as const
const TREE_ENTRY_KEYS = ['name', 'type', 'size', 'ext'] as const
const SEARCH_RESULT_KEYS = ['name', 'path', 'type', 'size', 'ext'] as const

function assertTreeEntry(raw: unknown, ctx: string): WorkspaceFileEntry {
  const o = reqObj(raw, ctx)
  reqExactKeys(o, TREE_ENTRY_KEYS, ctx)
  return {
    name: reqString(o.name, `${ctx}.name`),
    type: reqEnum(o.type, `${ctx}.type`, FILE_TYPES),
    size: reqNonNegativeInt(o.size, `${ctx}.size`),
    ext: reqStringAllowEmpty(o.ext, `${ctx}.ext`),
  }
}

export function assertWorkspaceFilesData(raw: unknown): WorkspaceFilesData {
  const o = reqObj(raw, 'files')
  reqExactKeys(o, ['path', 'entries'], 'files')
  if (!Array.isArray(o.entries)) fail('files.entries')
  return {
    path: reqStringAllowEmpty(o.path, 'files.path'),
    entries: (o.entries as unknown[]).map((e, i) => assertTreeEntry(e, `files.entries[${i}]`)),
  }
}

export function assertWorkspaceFileContentData(raw: unknown): WorkspaceFileContentData {
  const o = reqObj(raw, 'files/content')
  const binary = reqBool(o.binary, 'files/content.binary')
  if (binary) {
    reqExactKeys(o, ['path', 'size', 'binary'], 'files/content')
    return {
      path: reqString(o.path, 'files/content.path'),
      size: reqNonNegativeInt(o.size, 'files/content.size'),
      binary: true,
    }
  }
  reqExactKeys(o, ['path', 'size', 'binary', 'text'], 'files/content')
  return {
    path: reqString(o.path, 'files/content.path'),
    size: reqNonNegativeInt(o.size, 'files/content.size'),
    binary: false,
    text: reqStringAllowEmpty(o.text, 'files/content.text'),
  }
}

export function assertWorkspaceFileSearchData(raw: unknown): WorkspaceFileSearchData {
  const o = reqObj(raw, 'files/search')
  reqExactKeys(o, ['path', 'query', 'results', 'truncated'], 'files/search')
  if (!Array.isArray(o.results)) fail('files/search.results')
  return {
    path: reqStringAllowEmpty(o.path, 'files/search.path'),
    query: reqStringAllowEmpty(o.query, 'files/search.query'),
    results: (o.results as unknown[]).map((e, i) => {
      const r = reqObj(e, `files/search.results[${i}]`)
      reqExactKeys(r, SEARCH_RESULT_KEYS, `files/search.results[${i}]`)
      return {
        name: reqString(r.name, `files/search.results[${i}].name`),
        path: reqStringAllowEmpty(r.path, `files/search.results[${i}].path`),
        type: reqEnum(r.type, `files/search.results[${i}].type`, FILE_TYPES),
        size: reqNonNegativeInt(r.size, `files/search.results[${i}].size`),
        ext: reqStringAllowEmpty(r.ext, `files/search.results[${i}].ext`),
      }
    }),
    truncated: reqBool(o.truncated, 'files/search.truncated'),
  }
}

// ---------- legacy 裸形状守卫 ----------

const WORKBENCH_KEYS = ['project', 'assignments', 'sessions', 'source'] as const
const WORKBENCH_PROJECT_KEYS = ['id', 'slug', 'created_at'] as const
const ENV_CHECK_KEYS = ['herdr', 'agents', 'agent_mail'] as const

export function assertLegacyWorkbench(raw: unknown): LegacyWorkbench {
  const o = reqObj(raw, 'workbench')
  reqExactKeys(o, WORKBENCH_KEYS, 'workbench')
  const project = reqObj(o.project, 'workbench.project')
  reqExactKeys(project, WORKBENCH_PROJECT_KEYS, 'workbench.project')
  if (typeof project.id !== 'number' || !Number.isInteger(project.id)) fail('workbench.project.id')
  const createdAt = project.created_at
  if (createdAt !== null && typeof createdAt !== 'string' && typeof createdAt !== 'number') {
    fail('workbench.project.created_at')
  }
  if (!Array.isArray(o.assignments)) fail('workbench.assignments')
  const assignments = (o.assignments as unknown[]).map((row, i) =>
    reqObj(row, `workbench.assignments[${i}]`) as LegacyAssignment,
  )
  if (!Array.isArray(o.sessions)) fail('workbench.sessions')
  const sessions = (o.sessions as unknown[]).map((row, i): LegacySession => {
    const s = reqObj(row, `workbench.sessions[${i}]`)
    const panes = s.panes === undefined ? [] : s.panes
    if (!Array.isArray(panes)) fail(`workbench.sessions[${i}].panes`)
    return {
      session: reqString(s.session, `workbench.sessions[${i}].session`),
      status: s.status,
      focused_pane_id: s.focused_pane_id,
      panes: (panes as unknown[]).map((p, j) =>
        reqObj(p, `workbench.sessions[${i}].panes[${j}]`) as LegacyPane,
      ),
    }
  })
  const source = reqObj(o.source, 'workbench.source')
  const observedAt = source.observed_at
  if (observedAt !== null && observedAt !== undefined && typeof observedAt !== 'string' && typeof observedAt !== 'number') {
    fail('workbench.source.observed_at')
  }
  return {
    project: {
      id: project.id as number,
      slug: reqString(project.slug, 'workbench.project.slug'),
      created_at: (createdAt ?? null) as string | number | null,
    },
    assignments,
    sessions,
    source: {
      available: reqBool(source.available, 'workbench.source.available'),
      degraded: reqBool(source.degraded, 'workbench.source.degraded'),
      observed_at: (observedAt ?? null) as string | number | null,
    },
  }
}

export function assertLegacyEnvCheck(raw: unknown): LegacyEnvCheck {
  const o = reqObj(raw, 'env-check')
  reqExactKeys(o, ENV_CHECK_KEYS, 'env-check')
  const item = (v: unknown, ctx: string): LegacyEnvCheckItem => {
    const io = reqObj(v, ctx)
    return {
      installed: reqBool(io.installed, `${ctx}.installed`),
      path: reqStringAllowEmpty(io.path, `${ctx}.path`),
    }
  }
  const agentsRaw = reqObj(o.agents, 'env-check.agents')
  const agents: Record<string, LegacyEnvCheckItem> = {}
  for (const [name, v] of Object.entries(agentsRaw)) {
    agents[name] = item(v, `env-check.agents.${name}`)
  }
  const mail = reqObj(o.agent_mail, 'env-check.agent_mail')
  const reason = mail.reason
  if (reason !== undefined && reason !== null && typeof reason !== 'string') fail('env-check.agent_mail.reason')
  return {
    herdr: item(o.herdr, 'env-check.herdr'),
    agents,
    agent_mail: {
      available: reqBool(mail.available, 'env-check.agent_mail.available'),
      reason: (reason ?? null) as string | null,
      read_available: optBool(mail.read_available, 'env-check.agent_mail.read_available'),
      write_available: optBool(mail.write_available, 'env-check.agent_mail.write_available'),
      write_reason:
        mail.write_reason === undefined
          ? undefined
          : reqNullableString(mail.write_reason, 'env-check.agent_mail.write_reason'),
    },
  }
}

// ---------- 窄 legacy adapter：仅 env-check 与 workbench 两条明确裸路由 ----------

interface LegacyErrorEnvelope {
  error?: {
    code?: string
    message?: string
    retryable?: boolean
    request_id?: string
    details?: unknown
  } | null
}

function codeForStatus(status: number): string {
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status >= 500) return 'server_error'
  return 'http_error'
}

/**
 * 裸 legacy GET：2xx 返回原文（由调用方形状守卫 fail-closed），错误通道兼容
 * G3 error envelope（含 2xx 内嵌 error）与 legacy {detail}。不得用于 G3 路由。
 */
export async function legacyGet(path: string): Promise<unknown> {
  let res: Response
  try {
    res = await fetch(path, { headers: { Accept: 'application/json' } })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }

  let body: unknown = null
  let jsonOk = true
  try {
    body = await res.json()
  } catch {
    jsonOk = false
  }

  if (isObj(body) && 'error' in body && (body as LegacyErrorEnvelope).error) {
    const e = (body as LegacyErrorEnvelope).error!
    throw new ApiError({
      code: e.code ?? codeForStatus(res.status),
      message: e.message ?? `请求失败（HTTP ${res.status}）`,
      retryable: e.retryable ?? res.status >= 500,
      requestId: e.request_id ?? null,
      status: res.status,
      details: e.details,
    })
  }

  if (!res.ok) {
    const detail = isObj(body) && typeof body.detail === 'string' ? body.detail : null
    throw new ApiError({
      code: codeForStatus(res.status),
      message: detail ?? `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }

  if (!jsonOk) {
    throw new ProtocolError('响应不是合法 JSON', { status: res.status })
  }
  return body
}

// ---------- hooks ----------

function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.retryable && failureCount < 2
}

export function useWorkspaceList(projectId: string | null, slug: string | null) {
  const q = useQuery({
    queryKey: ['local-ws-list', projectId],
    queryFn: async () => {
      const res = await apiGet<WorkspaceListData>(LOCAL_SLICE_API.workspaces(projectId!))
      return { ...res, data: assertWorkspaceListData(res.data) }
    },
    enabled: projectId != null,
    staleTime: 15_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, slug ? projectScope(slug) : GLOBAL_SCOPE)
  return q
}

function useWorkspaceScoped<T>(
  key: readonly unknown[],
  path: string | null,
  slug: string | null,
  workspaceId: string | null,
  parse: (raw: unknown) => T,
) {
  const scope: CapabilityScope =
    slug && workspaceId ? workspaceScope(slug, workspaceId) : GLOBAL_SCOPE
  const q = useQuery({
    queryKey: key,
    queryFn: async () => {
      const res = await apiGet<T>(path!)
      return { ...res, data: parse(res.data) }
    },
    enabled: path != null,
    staleTime: 15_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, scope)
  return q
}

export function useWorkspaceDetail(
  projectId: string | null,
  workspaceId: string | null,
  slug: string | null,
) {
  return useWorkspaceScoped(
    ['local-ws-detail', projectId, workspaceId],
    projectId && workspaceId ? LOCAL_SLICE_API.workspace(projectId, workspaceId) : null,
    slug,
    workspaceId,
    (raw) => assertWorkspaceSummary(raw, 'workspace-detail'),
  )
}

export function useWorkspaceFiles(
  projectId: string | null,
  workspaceId: string | null,
  path: string,
  slug: string | null,
  enabled: boolean,
) {
  const base = projectId && workspaceId ? LOCAL_SLICE_API.workspaceFiles(projectId, workspaceId) : null
  const params = new URLSearchParams()
  params.set('path', path)
  return useWorkspaceScoped(
    ['local-ws-files', projectId, workspaceId, path],
    base && enabled ? `${base}?${params}` : null,
    slug,
    workspaceId,
    assertWorkspaceFilesData,
  )
}

export function useWorkspaceFileContent(
  projectId: string | null,
  workspaceId: string | null,
  file: string | null,
  slug: string | null,
  enabled: boolean,
) {
  const base =
    projectId && workspaceId ? LOCAL_SLICE_API.workspaceFileContent(projectId, workspaceId) : null
  const params = new URLSearchParams()
  if (file != null) params.set('path', file)
  return useWorkspaceScoped(
    ['local-ws-file-content', projectId, workspaceId, file],
    base && enabled && file != null ? `${base}?${params}` : null,
    slug,
    workspaceId,
    assertWorkspaceFileContentData,
  )
}

export function useWorkspaceFileSearch(
  projectId: string | null,
  workspaceId: string | null,
  path: string,
  q: string,
  slug: string | null,
  enabled: boolean,
) {
  const base =
    projectId && workspaceId ? LOCAL_SLICE_API.workspaceFileSearch(projectId, workspaceId) : null
  const params = new URLSearchParams()
  params.set('path', path)
  params.set('q', q)
  params.set('limit', '50')
  return useWorkspaceScoped(
    ['local-ws-file-search', projectId, workspaceId, path, q],
    base && enabled && q !== '' ? `${base}?${params}` : null,
    slug,
    workspaceId,
    assertWorkspaceFileSearchData,
  )
}

export function useLegacyWorkbench(slug: string | null) {
  return useQuery({
    queryKey: ['legacy-workbench', slug],
    queryFn: async () => assertLegacyWorkbench(await legacyGet(LOCAL_SLICE_API.legacyWorkbench(slug!))),
    enabled: slug != null,
    staleTime: 15_000,
    retry: shouldRetry,
  })
}

export function useLegacyEnvCheck() {
  return useQuery({
    queryKey: ['legacy-env-check'],
    queryFn: async () => assertLegacyEnvCheck(await legacyGet(LOCAL_SLICE_API.legacyEnvCheck)),
    staleTime: 60_000,
    retry: shouldRetry,
  })
}
