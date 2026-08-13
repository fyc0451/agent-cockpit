// WEB-003 Project Registry / runtime-nodes / discovery 的端点与类型唯一硬编码点。
// 形状照抄 api-freeze-v1 §2；纪律：G3 envelope 与必填字段绝不宽容（运行时守卫
// ProtocolError），宽容仅限 freeze 标注 [未冻结-上游] 的 optional 字段。

import { useMutation, useQuery } from '@tanstack/react-query'
import { GLOBAL_SCOPE, useReportCapabilities } from '../state/capabilities'
import { ApiError, ProtocolError, apiGet } from './client'
import type { ApiResult } from './client'
import type { ResponseMeta } from './types'

export const REGISTRY_API = {
  runtimeNodes: '/api/runtime-nodes',
  roots: (nodeId: string) => `/api/runtime-nodes/${encodeURIComponent(nodeId)}/roots`,
  directories: (nodeId: string) => `/api/runtime-nodes/${encodeURIComponent(nodeId)}/directories`,
  discovery: '/api/project-discovery',
  projects: '/api/project-registry/projects',
} as const

// ---------- runtime-nodes（W1 部分冻结） ----------
export interface RuntimeNode {
  node_id: string // v1 恒 "local"
  display_name: string
  kind?: string // [未冻结-上游] 非 "local" → disabled
  availability?: string // [未冻结-上游] 非 "available" → disabled
  reason?: string | null
}
export interface RuntimeNodesData {
  nodes: RuntimeNode[]
}

// ---------- roots（冻结 W2） ----------
export interface RootDescriptor {
  node_id: string
  root_id: string // ^root_[0-9a-f]{24}$
  display_name: string
}
export interface RootsData {
  items: RootDescriptor[]
}

// ---------- directories（冻结 W3 + c11844d 状态字段） ----------
export interface RegistryMatch {
  project_id: string
  slug: string
  display_name: string
}
export interface DirectoryEntry {
  name: string
  path: string // 规范 POSIX 相对路径
  kind: 'directory'
  vcs_hint: 'git' | 'unknown'
  registered_project?: RegistryMatch | null // [未冻结-上游]；partial=true 时 null 语义是「未知」
}
export interface DirectoryListingData {
  locator: ProjectLocator
  entries: DirectoryEntry[]
  complete: boolean
  partial: boolean // registry lookup 不可用 → 本地目录仍渲染 + degraded
  sources: string[]
  warnings: string[]
}

// ---------- locator / discovery（冻结） ----------
export interface ProjectLocator {
  node_id: string
  root_id: string
  path: string // 相对路径；禁绝对/~/./../空段/尾斜杠/反斜杠/NUL
}
export interface VcsObservation {
  // PROJ-002 c11844d public dict：无 raw branch/upstream/refs 名，只有布尔与计数/摘要
  kind: 'git' | 'none' // 永不 partial（W4）
  git_root_digest: string | null
  remote_fingerprint: string | null
  repository_fingerprint: string | null
  head: string | null
  branch_present: boolean
  detached: boolean
  unborn: boolean
  dirty: boolean
  status_digest: string | null
  refs_digest: string | null
  refs_count: number
  upstream_present: boolean
  ahead: number | null
  behind: number | null
}
export interface DiscoveryResultData {
  locator: ProjectLocator
  display_path: string
  canonical_path_digest: string
  vcs: VcsObservation
  exact_match: RegistryMatch | null
  possible_projects: RegistryMatch[]
  discovery_fingerprint: string // sha256:<64hex>
  observed_at: string // ISO +00:00 偏移（非 Z）
  complete: boolean
  sources: string[]
  warnings: string[]
}

// ---------- registry projects（冻结 + optional 内嵌） ----------
// accepted _public_location 精确七键；canonical_path 属内部根表示，禁止进入 public DTO
export interface RepoLocationSummary {
  repo_location_id: string
  project_id: string
  node_id: string
  vcs_kind: 'git' | 'none'
  availability: 'available' | 'offline' | 'missing' | 'unknown'
  lifecycle: 'active' | 'archived'
  version: number
}
export interface RegistryProject {
  project_id: string
  slug: string
  display_name: string
  goal: string | null
  lifecycle: 'active' | 'archived'
  version: number
  created_at: string
  updated_at: string
  repo_locations?: RepoLocationSummary[] // [未冻结-上游] 列表是否内嵌
}
export interface ProjectListData {
  items: RegistryProject[]
  next_cursor: string | null // 非 null → degraded，不翻页
}

// ---------- register（最小集冻结 + 宽容扩展） ----------
export interface RegisterProjectRequest {
  display_name: string
  slug: string
  goal?: string | null
  locator: ProjectLocator
  expected_discovery_fingerprint: string
}
export interface RegisterProjectData {
  project_id: string
  slug: string
  project?: RegistryProject // [未冻结-上游]
  repo_location?: RepoLocationSummary // [未冻结-上游]
}

// ---------- 必填字段运行时守卫（fail-closed：缺/错 → ProtocolError） ----------

function fail(field: string): never {
  throw new ProtocolError(`registry 响应必填字段缺失或类型错误：${field}`)
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

/** locator.path 允许空串（root 级目录） */
function reqStringAllowEmpty(v: unknown, field: string): string {
  if (typeof v !== 'string') fail(field)
  return v
}

function reqBool(v: unknown, field: string): boolean {
  if (typeof v !== 'boolean') fail(field)
  return v
}

function reqInt(v: unknown, field: string): number {
  if (typeof v !== 'number' || !Number.isInteger(v)) fail(field)
  return v
}

function reqNullableString(v: unknown, field: string): string | null {
  if (v === null) return null
  if (typeof v !== 'string') fail(field)
  return v
}

function reqNullableInt(v: unknown, field: string): number | null {
  if (v === null) return null
  if (typeof v !== 'number' || !Number.isInteger(v)) fail(field)
  return v
}

function reqStringArray(v: unknown, field: string): string[] {
  const arr = reqArray(v, field)
  for (let i = 0; i < arr.length; i++) {
    if (typeof arr[i] !== 'string') fail(`${field}[${i}]`)
  }
  return arr as string[]
}

/** RegistryMatch 守卫：project_id/slug/display_name 均为 string */
function assertRegistryMatch(raw: unknown, ctx: string): RegistryMatch {
  const o = reqObj(raw, ctx)
  return {
    project_id: reqString(o.project_id, `${ctx}.project_id`),
    slug: reqString(o.slug, `${ctx}.slug`),
    display_name: reqString(o.display_name, `${ctx}.display_name`),
  }
}

/** nullable RegistryMatch：null/undefined → null；其他任何非对象/错型 → ProtocolError */
function assertNullableMatch(raw: unknown, ctx: string): RegistryMatch | null {
  if (raw === null || raw === undefined) return null
  return assertRegistryMatch(raw, ctx)
}

function reqObj(v: unknown, field: string): Record<string, unknown> {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) fail(field)
  return v as Record<string, unknown>
}

function reqArray(v: unknown, field: string): unknown[] {
  if (!Array.isArray(v)) fail(field)
  return v
}

export function assertLocator(raw: unknown, ctx = 'locator'): ProjectLocator {
  const o = reqObj(raw, ctx)
  return {
    node_id: reqString(o.node_id, `${ctx}.node_id`),
    root_id: reqString(o.root_id, `${ctx}.root_id`),
    path: reqStringAllowEmpty(o.path, `${ctx}.path`),
  }
}

/** public repo location 守卫：accepted 精确七键 fail-closed（多键少键都拒绝，禁 canonical_path） */
const REPO_LOCATION_KEYS = [
  'repo_location_id',
  'project_id',
  'node_id',
  'lifecycle',
  'vcs_kind',
  'availability',
  'version',
] as const

export function assertRepoLocationSummary(raw: unknown, ctx = 'repo_location'): RepoLocationSummary {
  const o = reqObj(raw, ctx)
  const actual = Object.keys(o).sort()
  const expected = [...REPO_LOCATION_KEYS].sort()
  if (actual.length !== expected.length || actual.some((k, i) => k !== expected[i])) {
    fail(`${ctx} 键集`)
  }
  const vcsKind = o.vcs_kind
  if (vcsKind !== 'git' && vcsKind !== 'none') fail(`${ctx}.vcs_kind`)
  const availability = o.availability
  if (
    availability !== 'available' && availability !== 'offline' &&
    availability !== 'missing' && availability !== 'unknown'
  ) {
    fail(`${ctx}.availability`)
  }
  const lifecycle = o.lifecycle
  if (lifecycle !== 'active' && lifecycle !== 'archived') fail(`${ctx}.lifecycle`)
  return {
    repo_location_id: reqString(o.repo_location_id, `${ctx}.repo_location_id`),
    project_id: reqString(o.project_id, `${ctx}.project_id`),
    node_id: reqString(o.node_id, `${ctx}.node_id`),
    vcs_kind: vcsKind,
    availability,
    lifecycle,
    version: reqInt(o.version, `${ctx}.version`),
  }
}

function reqRepoLocationArray(raw: unknown, ctx: string): RepoLocationSummary[] {
  return reqArray(raw, ctx).map((item, i) => assertRepoLocationSummary(item, `${ctx}[${i}]`))
}

export function assertRegistryProject(raw: unknown): RegistryProject {
  const o = reqObj(raw, 'project')
  const project: RegistryProject = {
    project_id: reqString(o.project_id, 'project.project_id'),
    slug: reqString(o.slug, 'project.slug'),
    display_name: reqString(o.display_name, 'project.display_name'),
    goal: typeof o.goal === 'string' ? o.goal : null,
    lifecycle: o.lifecycle === 'archived' ? 'archived' : 'active',
    version: typeof o.version === 'number' ? o.version : 0,
    created_at: typeof o.created_at === 'string' ? o.created_at : '',
    updated_at: typeof o.updated_at === 'string' ? o.updated_at : '',
  }
  if (o.repo_locations !== undefined) {
    project.repo_locations = reqRepoLocationArray(o.repo_locations, 'project.repo_locations')
  }
  return project
}

/**
 * SLICE-001：真实 data.items 元素是 { project, repo_locations } 嵌套快照
 * （GET /api/project-registry/projects 的 _snapshot 形状）。解析后摊平成
 * RegistryProject（repo_locations 内嵌），页面消费形态不变。
 */
export function assertProjectListItem(raw: unknown, ctx: string): RegistryProject {
  const o = reqObj(raw, ctx)
  const project = assertRegistryProject(o.project)
  const locations = o.repo_locations !== undefined
    ? reqRepoLocationArray(o.repo_locations, `${ctx}.repo_locations`)
    : project.repo_locations
  return { ...project, repo_locations: locations ?? [] }
}

export function assertProjectListData(raw: unknown): ProjectListData {
  const o = reqObj(raw, 'projects')
  return {
    items: reqArray(o.items, 'projects.items').map((item, i) =>
      assertProjectListItem(item, `projects.items[${i}]`),
    ),
    next_cursor: typeof o.next_cursor === 'string' ? o.next_cursor : null,
  }
}

export function assertRuntimeNodesData(raw: unknown): RuntimeNodesData {
  const o = reqObj(raw, 'runtime-nodes')
  return {
    nodes: reqArray(o.nodes, 'runtime-nodes.nodes').map((n) => {
      const node = reqObj(n, 'node')
      return {
        node_id: reqString(node.node_id, 'node.node_id'),
        display_name: reqString(node.display_name, 'node.display_name'),
        kind: typeof node.kind === 'string' ? node.kind : undefined,
        availability: typeof node.availability === 'string' ? node.availability : undefined,
        reason: typeof node.reason === 'string' ? node.reason : null,
      }
    }),
  }
}

export function assertRootsData(raw: unknown): RootsData {
  const o = reqObj(raw, 'roots')
  return {
    items: reqArray(o.items, 'roots.items').map((r) => {
      const root = reqObj(r, 'root')
      return {
        node_id: reqString(root.node_id, 'root.node_id'),
        root_id: reqString(root.root_id, 'root.root_id'),
        display_name: reqString(root.display_name, 'root.display_name'),
      }
    }),
  }
}

export function assertDirectoryListingData(raw: unknown): DirectoryListingData {
  const o = reqObj(raw, 'directories')
  return {
    locator: assertLocator(o.locator, 'directories.locator'),
    entries: reqArray(o.entries, 'directories.entries').map((e, i) => {
      const entry = reqObj(e, 'entry')
      const vcsHint = entry.vcs_hint === 'git' ? 'git' : 'unknown'
      return {
        name: reqString(entry.name, 'entry.name'),
        path: reqString(entry.path, 'entry.path'),
        kind: 'directory' as const,
        vcs_hint: vcsHint,
        registered_project: assertNullableMatch(
          entry.registered_project,
          `directories.entries[${i}].registered_project`,
        ),
      }
    }),
    complete: reqBool(o.complete, 'directories.complete'),
    partial: reqBool(o.partial, 'directories.partial'),
    sources: reqStringArray(o.sources, 'directories.sources'),
    warnings: reqStringArray(o.warnings, 'directories.warnings'),
  }
}

export function assertDiscoveryResultData(raw: unknown): DiscoveryResultData {
  const o = reqObj(raw, 'discovery')
  const vcs = reqObj(o.vcs, 'discovery.vcs')
  const vcsKind = vcs.kind // 必填：缺失/非 git|none → ProtocolError（W4：vcs 永不 partial）
  if (vcsKind !== 'git' && vcsKind !== 'none') fail('discovery.vcs.kind')
  // c11844d public dict 逐字段重建（fail-closed；额外字段容忍但不带回）
  const vcsChecked: VcsObservation = {
    kind: vcsKind,
    git_root_digest: reqNullableString(vcs.git_root_digest, 'discovery.vcs.git_root_digest'),
    remote_fingerprint: reqNullableString(vcs.remote_fingerprint, 'discovery.vcs.remote_fingerprint'),
    repository_fingerprint: reqNullableString(vcs.repository_fingerprint, 'discovery.vcs.repository_fingerprint'),
    head: reqNullableString(vcs.head, 'discovery.vcs.head'),
    branch_present: reqBool(vcs.branch_present, 'discovery.vcs.branch_present'),
    detached: reqBool(vcs.detached, 'discovery.vcs.detached'),
    unborn: reqBool(vcs.unborn, 'discovery.vcs.unborn'),
    dirty: reqBool(vcs.dirty, 'discovery.vcs.dirty'),
    status_digest: reqNullableString(vcs.status_digest, 'discovery.vcs.status_digest'),
    refs_digest: reqNullableString(vcs.refs_digest, 'discovery.vcs.refs_digest'),
    refs_count: reqInt(vcs.refs_count, 'discovery.vcs.refs_count'),
    upstream_present: reqBool(vcs.upstream_present, 'discovery.vcs.upstream_present'),
    ahead: reqNullableInt(vcs.ahead, 'discovery.vcs.ahead'),
    behind: reqNullableInt(vcs.behind, 'discovery.vcs.behind'),
  }
  return {
    locator: assertLocator(o.locator, 'discovery.locator'),
    display_path: reqString(o.display_path, 'discovery.display_path'),
    canonical_path_digest: reqString(o.canonical_path_digest, 'discovery.canonical_path_digest'),
    vcs: vcsChecked,
    exact_match: assertNullableMatch(o.exact_match, 'discovery.exact_match'),
    possible_projects: reqArray(o.possible_projects, 'discovery.possible_projects').map((m, i) =>
      assertRegistryMatch(m, `discovery.possible_projects[${i}]`),
    ),
    discovery_fingerprint: reqString(o.discovery_fingerprint, 'discovery.discovery_fingerprint'),
    observed_at: reqString(o.observed_at, 'discovery.observed_at'),
    complete: reqBool(o.complete, 'discovery.complete'),
    sources: reqStringArray(o.sources, 'discovery.sources'),
    warnings: reqStringArray(o.warnings, 'discovery.warnings'),
  }
}

export function assertRegisterProjectData(raw: unknown): RegisterProjectData {
  const o = reqObj(raw, 'register')
  return {
    project_id: reqString(o.project_id, 'register.project_id'),
    slug: reqString(o.slug, 'register.slug'),
    project: o.project ? assertRegistryProject(o.project) : undefined,
    repo_location: o.repo_location ? (o.repo_location as RepoLocationSummary) : undefined,
  }
}

// ---------- apiPost（client.ts strict 块照抄 + POST/Idempotency-Key；client.ts 抽出共用 helper 后迁移） ----------

export async function apiPost<TReq, TRes>(
  path: string,
  body: TReq,
  opts: { idempotencyKey?: string } = {},
): Promise<ApiResult<TRes>> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...(opts.idempotencyKey ? { 'Idempotency-Key': opts.idempotencyKey } : {}),
      },
      body: JSON.stringify(body),
    })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }

  let parsed: unknown = null
  let jsonOk = true
  try {
    parsed = await res.json()
  } catch {
    jsonOk = false
  }

  if (parsed && typeof parsed === 'object' && 'error' in parsed && (parsed as { error?: unknown }).error) {
    const e = (parsed as { error: { code?: string; message?: string; retryable?: boolean; request_id?: string; details?: unknown } }).error
    throw new ApiError({
      code: e.code ?? (res.status >= 500 ? 'server_error' : 'http_error'),
      message: e.message ?? `请求失败（HTTP ${res.status}）`,
      retryable: e.retryable ?? res.status >= 500,
      requestId: e.request_id ?? null,
      status: res.status,
      details: e.details,
    })
  }

  if (!res.ok) {
    throw new ApiError({
      code: res.status === 409 ? 'conflict' : res.status >= 500 ? 'server_error' : 'http_error',
      message: `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }

  // G3 严格校验（与 apiGet 一致）：2xx 必须是完整 { data, meta } envelope
  if (!jsonOk) throw new ProtocolError('响应不是合法 JSON', { status: res.status })
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ProtocolError('响应不是 G3 envelope（裸 body 不允许透传）', { status: res.status })
  }
  if (!('data' in parsed)) throw new ProtocolError('响应 envelope 缺少 data 键', { status: res.status })
  if (!('meta' in parsed)) throw new ProtocolError('响应 envelope 缺少 meta 键', { status: res.status })
  const env = parsed as { data: TRes; meta?: ResponseMeta }
  return { data: env.data, meta: env.meta ?? null }
}

// ---------- hooks（模式照 api/hooks.ts：queryKey 首段定 scope + meta.capabilities 上报） ----------

function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.retryable && failureCount < 2
}

export function useProjectRegistryList() {
  const q = useQuery({
    queryKey: ['registry-projects'],
    queryFn: async () => {
      const res = await apiGet<ProjectListData>(REGISTRY_API.projects)
      return { ...res, data: assertProjectListData(res.data) }
    },
    staleTime: 15_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, GLOBAL_SCOPE)
  return q
}

export function useRuntimeNodes() {
  const q = useQuery({
    queryKey: ['runtime-nodes'],
    queryFn: async () => {
      const res = await apiGet<RuntimeNodesData>(REGISTRY_API.runtimeNodes)
      return { ...res, data: assertRuntimeNodesData(res.data) }
    },
    staleTime: 30_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, GLOBAL_SCOPE)
  return q
}

export function useNodeRoots(nodeId: string | null) {
  const q = useQuery({
    queryKey: ['runtime-roots', nodeId],
    queryFn: async () => {
      const res = await apiGet<RootsData>(REGISTRY_API.roots(nodeId!))
      return { ...res, data: assertRootsData(res.data) }
    },
    enabled: nodeId != null,
    staleTime: 30_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, GLOBAL_SCOPE)
  return q
}

export function useNodeDirectories(locator: ProjectLocator | null) {
  const q = useQuery({
    queryKey: ['runtime-dirs', locator?.node_id ?? null, locator?.root_id ?? null, locator?.path ?? null],
    queryFn: async () => {
      const params = new URLSearchParams()
      params.set('root_id', locator!.root_id)
      params.set('path', locator!.path)
      const res = await apiGet<DirectoryListingData>(`${REGISTRY_API.directories(locator!.node_id)}?${params}`)
      return { ...res, data: assertDirectoryListingData(res.data) }
    },
    enabled: locator != null,
    staleTime: 15_000,
    retry: shouldRetry,
  })
  useReportCapabilities(q.data?.meta, GLOBAL_SCOPE)
  return q
}

/** 只读探测用 mutation：结果要锁定在向导状态里，不随 query key 漂移 */
export function useProjectDiscovery() {
  return useMutation({
    mutationFn: async (locator: ProjectLocator) => {
      const res = await apiPost<{ locator: ProjectLocator }, DiscoveryResultData>(REGISTRY_API.discovery, {
        locator,
      })
      return { ...res, data: assertDiscoveryResultData(res.data) }
    },
  })
}

export function useRegisterProject() {
  return useMutation({
    mutationFn: async (vars: { req: RegisterProjectRequest; idempotencyKey: string }) => {
      const res = await apiPost<RegisterProjectRequest, RegisterProjectData>(REGISTRY_API.projects, vars.req, {
        idempotencyKey: vars.idempotencyKey,
      })
      return { ...res, data: assertRegisterProjectData(res.data) }
    },
  })
}
