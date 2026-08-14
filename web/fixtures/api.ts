import type { Project } from '../api/types'

/** 测试用后端载荷（fixtures，不是演示数据——页面本身不消费这些）
 *
 * SLICE-001 真实化：
 * - Registry list 的 data.items 是真实嵌套快照 { project, repo_locations }；
 *   public repo location 精确七键（禁 canonical_path）。
 * - Workspace 用 persisted workspace_id（w1/w2 为冻结测试 ID；身份权威是
 *   list/detail 端点，不是 Project fixture 嵌入数组）；public DTO 无 canonical root。
 * - legacy workbench / env-check 为裸形状（非 G3 envelope）。
 * - Files 为 Workspace-scoped G3，只含相对 path；默认世界 files.read 关闭。
 */

export const projectP1: Project = {
  slug: 'p1',
  name: 'Project One',
  branch: 'main',
  path: '/repos/p1',
  workspaces: [
    { id: 'w1', name: '本机工作区', location: 'local', branch: 'main', status: 'running' },
    { id: 'w2', name: '远程 GPU', location: 'remote', branch: 'dev', status: 'stopped' },
  ],
}

/** 完整 G3 聚合元数据（data+meta+sources/capabilities） */
export const metaOk = {
  request_id: 'req-test-1',
  generated_at: '2026-08-12T00:00:00Z',
  sources: [{ name: 'herdr', status: 'available', observed_at: '2026-08-12T00:00:00Z', reason: null }],
  capabilities: {},
}

export const agentMailStatus = {
  available: true,
  reason: null,
  read_available: true,
  write_available: true,
  write_reason: null,
}

export const overviewPayload = {
  projects: [{
    id: 1,
    slug: 'p1',
    human_key: '/repos/p1',
    agent_count: 0,
    active_agent_count: 0,
    message_count: 0,
    last_activity: null,
    unread: 0,
  }],
  total_unread: 0,
  total_projects: 1,
  total_agents: 0,
  agent_mail: agentMailStatus,
}

export const attentionPayload = {
  sessions: [],
  items: [],
  count: 0,
  mail_unread: 0,
  capabilities: { agent_mail: agentMailStatus },
}

export const settingsPayload = {
  language: 'zh',
  dir_agents: {},
  enabled_agents: ['codex', 'kimi', 'claude', 'qodercli', 'grok', 'opencode'],
  upload_max_mb: 100,
  team_hub_url: '',
  human_auth_url: '',
  term: { max_terms: 16, idle_ttl: 1800, write_timeout: 2.0 },
  known_agents: ['codex', 'kimi', 'claude', 'qodercli', 'grok', 'opencode'],
  languages: ['zh', 'en', 'ja'],
}

export const herdrStatusPayload = {
  available: true,
  binary: '/usr/local/bin/herdr',
}

export const tasksPayload = [
  {
    id: 'task-e2e-1',
    workdir: '/repos/p1',
    source_workdir: '/repos/p1',
    base_sha: '0123456789abcdef0123456789abcdef01234567',
    run_workdir: '/repos/.agent-cockpit-worktrees/task-e2e-1',
    preview_hash: null,
    prompt: '修复登录回归',
    model: 'codex',
    status: 'pending',
    pid: null,
    exit_code: null,
    created_ts: 1,
    started_ts: null,
    finished_ts: null,
    output_lines: 0,
  },
]

// ================= SLICE-001：真实 Registry 身份 =================

export const REG_P1 = `prj_${'11'.repeat(16)}`
export const REG_P2 = `prj_${'22'.repeat(16)}`

// ================= SLICE-001：legacy 裸形状（workbench 严格四键 / env-check 严格三键） =================

/** 裸 legacy workbench（非 envelope）：project 严格三键 + assignments/sessions/source */
export const legacyWorkbenchPayload = {
  project: { id: 7, slug: 'p1', created_at: '2026-08-01T00:00:00+00:00' },
  assignments: [
    {
      assignment_id: 'asg-1',
      assignment: '修复登录回归',
      assignee: 'kimi',
      expected_reply: '',
      deadline: null,
      status: 'working',
      closed_at: null,
      version: 1,
      created_at: '2026-08-12T09:00:00+00:00',
      updated_at: '2026-08-12T10:00:00+00:00',
    },
  ],
  sessions: [
    {
      session: 'main',
      status: 'running',
      focused_pane_id: '%1',
      panes: [
        { pane_id: '%1', agent: 'codex', agent_status: 'running', focused: true, revision: 3 },
      ],
    },
  ],
  source: { available: true, degraded: false, observed_at: '2026-08-13T10:00:00+00:00' },
}

/** source degraded 变体：Herdr 不可用 → sessions=[] + degraded=true（仍 200） */
export const legacyWorkbenchDegradedPayload = {
  ...legacyWorkbenchPayload,
  sessions: [],
  source: { available: false, degraded: true, observed_at: '2026-08-13T10:00:00+00:00' },
}

/** 裸 legacy env-check（非 envelope）：严格 {herdr,agents,agent_mail} */
export const legacyEnvCheckPayload = {
  herdr: { installed: false, path: '' },
  agents: {
    codex: { installed: true, path: '/usr/local/bin/codex' },
    kimi: { installed: true, path: '/usr/local/bin/kimi' },
    claude: { installed: false, path: '' },
    qodercli: { installed: false, path: '' },
    grok: { installed: false, path: '' },
    opencode: { installed: false, path: '' },
  },
  agent_mail: {
    available: true,
    reason: null,
    read_available: true,
    write_available: true,
    write_reason: null,
  },
}

// ================= SLICE-001：persisted Workspaces（G3，public DTO 精确 12 键，无 canonical root） =================

const wsBase = {
  goal: null,
  isolation_kind: 'shared',
  lifecycle: 'active',
  active_run_id: null,
  version: 1,
  created_at: '2026-08-10T00:00:00+00:00',
  updated_at: '2026-08-10T00:00:00+00:00',
}

export const workspaceW1 = {
  workspace_id: 'w1',
  project_id: REG_P1,
  repo_location_id: `loc_${'1a'.repeat(16)}`,
  name: '本机工作区',
  ...wsBase,
  repo_location: { node_id: 'local', availability: 'available' },
}

export const workspaceW2 = {
  workspace_id: 'w2',
  project_id: REG_P1,
  repo_location_id: `loc_${'2b'.repeat(16)}`,
  name: '远程 GPU',
  ...wsBase,
  repo_location: { node_id: 'gpu-1', availability: 'available' },
}

export const workspaceP2W1 = {
  workspace_id: 'w1',
  project_id: REG_P2,
  repo_location_id: `loc_${'3c'.repeat(16)}`,
  name: 'B 工作区',
  ...wsBase,
  repo_location: { node_id: 'local', availability: 'available' },
}

export const workspaceListP1Payload = {
  data: { items: [workspaceW1, workspaceW2] },
  meta: metaOk,
}

export const workspaceListP2Payload = {
  data: { items: [workspaceP2W1] },
  meta: metaOk,
}

export const workspaceDetailW1Payload = { data: workspaceW1, meta: metaOk }
export const workspaceDetailW2Payload = { data: workspaceW2, meta: metaOk }
export const workspaceDetailP2W1Payload = { data: workspaceP2W1, meta: metaOk }

/** workspace detail meta capabilities 权威值：files.read 开、terminal.pty 恒关（冻结 reason） */
export const workspaceCapsOpenMeta = {
  ...metaOk,
  capabilities: {
    'files.read': { available: true, reason: null },
    'terminal.pty': { available: false, reason: 'workspace_terminal_ticket_deferred' },
  },
}

export const workspaceDetailW1OpenPayload = { data: workspaceW1, meta: workspaceCapsOpenMeta }

// ================= SLICE-001：Workspace-scoped Files（G3，只含相对 path） =================
// tree={path,entries[{name,type,size,ext}]}；content={path,size,binary,text?}；
// search={path,query,results[{name,path,type,size,ext}],truncated}

export const wsFilesRootPayload = {
  data: {
    path: '',
    entries: [
      { name: 'src', type: 'dir', size: 0, ext: '' },
      { name: 'docs', type: 'dir', size: 0, ext: '' },
      { name: 'README.md', type: 'file', size: 128, ext: '.md' },
    ],
  },
  meta: metaOk,
}

export const wsFilesSrcPayload = {
  data: {
    path: 'src',
    entries: [
      { name: 'main.ts', type: 'file', size: 64, ext: '.ts' },
      { name: 'util.ts', type: 'file', size: 32, ext: '.ts' },
    ],
  },
  meta: metaOk,
}

/** degraded 变体：meta.sources 含非 available（tree data 无 partial/warnings 字段） */
export const wsFilesDegradedPayload = {
  data: {
    path: '',
    entries: [{ name: 'src', type: 'dir', size: 0, ext: '' }],
  },
  meta: {
    ...metaOk,
    partial: true,
    sources: [
      { name: 'project_registry', status: 'available', observed_at: null, reason: null },
      { name: 'local_files', status: 'stale', observed_at: null, reason: 'index_unavailable' },
    ],
  },
}

export const wsFileContentPayload = {
  data: { path: 'README.md', size: 128, binary: false, text: '# Project One\n本机只读预览。\n' },
  meta: metaOk,
}

export const wsFileContentBinaryPayload = {
  data: { path: 'docs/logo.png', size: 512, binary: true },
  meta: metaOk,
}

export const wsFileSearchPayload = {
  data: {
    path: '',
    query: 'main',
    results: [{ name: 'main.ts', path: 'src/main.ts', type: 'file', size: 64, ext: '.ts' }],
    truncated: false,
  },
  meta: metaOk,
}

// ================= 默认路由映射 =================

/** 默认路由 → 载荷映射，测试可在此基础上覆盖 */
export function defaultFetchMap(): Record<string, unknown> {
  return {
    '/api/overview': overviewPayload,
    '/api/attention': attentionPayload,
    '/api/env-check': legacyEnvCheckPayload,
    '/api/settings': settingsPayload,
    '/api/herdr/status': herdrStatusPayload,
    '/api/tasks': tasksPayload,
    // legacy workbench 裸形状（SLICE-001 窄 adapter）
    '/api/projects/p1/workbench': legacyWorkbenchPayload,
    // WEB-003 registry / runtime-nodes（GET；POST discovery/register 不进 map，由测试按 method 分支）
    '/api/project-registry/projects': registryProjectsPayload,
    '/api/runtime-nodes': runtimeNodesPayload,
    '/api/runtime-nodes/local/roots': rootsPayload,
    '/api/runtime-nodes/local/directories': directoriesPayload,
    // SLICE-001 persisted workspaces（G3）
    [`/api/project-registry/projects/${REG_P1}/workspaces`]: workspaceListP1Payload,
    [`/api/project-registry/projects/${REG_P2}/workspaces`]: workspaceListP2Payload,
    [`/api/project-registry/projects/${REG_P1}/workspaces/w1`]: workspaceDetailW1Payload,
    [`/api/project-registry/projects/${REG_P1}/workspaces/w2`]: workspaceDetailW2Payload,
    [`/api/project-registry/projects/${REG_P2}/workspaces/w1`]: workspaceDetailP2W1Payload,
    // SLICE-001 Workspace-scoped files（默认世界 files.read 关闭，正常不会被请求）
    [`/api/project-registry/projects/${REG_P1}/workspaces/w1/files`]: wsFilesRootPayload,
  }
}

// ================= WEB-003：Project Registry / discovery / runtime-nodes =================
// 全部为完整 G3 envelope（data+meta+sources/capabilities）；字段形状照 api-freeze-v1 §2，
// SLICE-001 起 list items 为真实嵌套快照 { project, repo_locations }。

export const REG_ROOT_CODE = 'root_0123456789abcdef01234567'
export const REG_ROOT_DOCS = 'root_f0123456789abcde01234567'

const REG_ALPHA = `prj_${'a1'.repeat(16)}`
const REG_BETA = `prj_${'c3'.repeat(16)}`

function registryProject(p: {
  project_id: string
  slug: string
  display_name: string
  goal?: string | null
  version?: number
  created_at?: string
  updated_at?: string
}) {
  return {
    project_id: p.project_id,
    slug: p.slug,
    display_name: p.display_name,
    goal: p.goal ?? null,
    lifecycle: 'active',
    version: p.version ?? 1,
    created_at: p.created_at ?? '2026-08-01T00:00:00+00:00',
    updated_at: p.updated_at ?? '2026-08-01T00:00:00+00:00',
  }
}

export const registryProjectsPayload = {
  data: {
    items: [
      {
        project: registryProject({
          project_id: REG_ALPHA,
          slug: 'alpha',
          display_name: 'Alpha 项目',
        }),
        repo_locations: [
          {
            repo_location_id: `loc_${'b2'.repeat(16)}`,
            project_id: REG_ALPHA,
            node_id: 'local',
            vcs_kind: 'git',
            availability: 'available',
            lifecycle: 'active',
            version: 1,
          },
        ],
      },
      {
        project: registryProject({
          project_id: REG_BETA,
          slug: 'beta',
          display_name: 'Beta 项目',
          goal: 'beta goal',
          version: 3,
          updated_at: '2026-08-03T00:00:00+00:00',
        }),
        repo_locations: [
          {
            repo_location_id: `loc_${'d4'.repeat(16)}`,
            project_id: REG_BETA,
            node_id: 'local',
            vcs_kind: 'none',
            availability: 'offline',
            lifecycle: 'active',
            version: 1,
          },
        ],
      },
      // SLICE-001：p1/p2 为既有深链/selection/capabilities 测试的 Registry 权威身份
      {
        project: registryProject({
          project_id: REG_P1,
          slug: 'p1',
          display_name: 'Project One',
          created_at: '2026-08-01T00:00:00+00:00',
        }),
        repo_locations: [],
      },
      {
        project: registryProject({
          project_id: REG_P2,
          slug: 'p2',
          display_name: 'Project Two',
          created_at: '2026-08-02T00:00:00+00:00',
        }),
        repo_locations: [],
      },
    ],
    next_cursor: null,
  },
  meta: metaOk,
}

export const registryProjectsEmptyPayload = {
  data: { items: [], next_cursor: null },
  meta: metaOk,
}

export const runtimeNodesPayload = {
  data: {
    nodes: [
      { node_id: 'local', display_name: '本机', kind: 'local', availability: 'available', reason: null },
      // fail-closed 样本：非 local 节点
      { node_id: 'gpu-1', display_name: '远程 GPU 节点', kind: 'ssh', availability: 'available', reason: null },
      // fail-closed 样本：离线节点
      { node_id: 'wsl-1', display_name: 'WSL 节点', kind: 'wsl', availability: 'offline', reason: '节点离线' },
    ],
  },
  meta: metaOk,
}

/** 黄金路径：两个可用 local 节点（不触发自动跳过，位置步照常渲染）+ disabled 样本 */
export const runtimeNodesMultiUsablePayload = {
  data: {
    nodes: [
      { node_id: 'local', display_name: '本机', kind: 'local', availability: 'available', reason: null },
      { node_id: 'local-2', display_name: '第二台本机', kind: 'local', availability: 'available', reason: null },
      { node_id: 'gpu-1', display_name: '远程 GPU 节点', kind: 'ssh', availability: 'available', reason: null },
      { node_id: 'wsl-1', display_name: 'WSL 节点', kind: 'wsl', availability: 'offline', reason: '节点离线' },
    ],
  },
  meta: metaOk,
}

export const rootsPayload = {
  data: {
    items: [
      { node_id: 'local', root_id: REG_ROOT_CODE, display_name: '代码' },
      { node_id: 'local', root_id: REG_ROOT_DOCS, display_name: '文档' },
    ],
  },
  meta: metaOk,
}

export const directoriesPayload = {
  data: {
    locator: { node_id: 'local', root_id: REG_ROOT_CODE, path: '' },
    entries: [
      { name: 'alpha', path: 'alpha', kind: 'directory', vcs_hint: 'git', registered_project: null },
      {
        name: 'beta',
        path: 'beta',
        kind: 'directory',
        vcs_hint: 'unknown',
        registered_project: {
          project_id: REG_BETA,
          slug: 'beta',
          display_name: 'Beta 项目',
        },
      },
    ],
    complete: true,
    partial: false,
    sources: ['local_files', 'project_registry'],
    warnings: [],
  },
  meta: metaOk,
}

/** B2：registry lookup 不可用 → partial=true；registered_project=null 语义是「未知」 */
export const directoriesPartialPayload = {
  data: {
    locator: { node_id: 'local', root_id: REG_ROOT_CODE, path: '' },
    entries: [
      { name: 'alpha', path: 'alpha', kind: 'directory', vcs_hint: 'git', registered_project: null },
      { name: 'beta', path: 'beta', kind: 'directory', vcs_hint: 'unknown', registered_project: null },
    ],
    complete: true,
    partial: true,
    sources: ['local_files'],
    warnings: ['project_registry_unavailable'],
  },
  meta: metaOk,
}

const vcsGit = {
  kind: 'git',
  git_root_digest: 'sha256:gitroot',
  remote_fingerprint: null,
  repository_fingerprint: 'sha256:repofp',
  head: 'abc1234',
  branch_present: true,
  detached: false,
  unborn: false,
  dirty: false,
  status_digest: null,
  refs_digest: 'sha256:refs',
  refs_count: 3,
  upstream_present: false,
  ahead: 0,
  behind: 0,
}

export const discoveryGitPayload = {
  data: {
    locator: { node_id: 'local', root_id: REG_ROOT_CODE, path: 'alpha' },
    display_path: '代码/alpha',
    canonical_path_digest: `sha256:${'1'.repeat(64)}`,
    vcs: vcsGit,
    exact_match: null,
    possible_projects: [],
    discovery_fingerprint: `sha256:${'a'.repeat(64)}`,
    observed_at: '2026-08-13T10:00:00+00:00',
    complete: true,
    sources: ['local_files', 'local_git', 'project_registry'],
    warnings: [],
  },
  meta: { ...metaOk, capabilities: { 'projectRegistry.write': true } },
}

export const discoveryPlainPayload = {
  data: {
    ...discoveryGitPayload.data,
    locator: { node_id: 'local', root_id: REG_ROOT_CODE, path: 'beta' },
    display_path: '代码/beta',
    vcs: { ...vcsGit, kind: 'none', git_root_digest: null, repository_fingerprint: null, head: null, branch_present: false, refs_count: 0 },
    discovery_fingerprint: `sha256:${'b'.repeat(64)}`,
  },
  meta: { ...metaOk, capabilities: { 'projectRegistry.write': true } },
}

export const discoveryDegradedPayload = {
  data: {
    ...discoveryGitPayload.data,
    complete: false,
    warnings: ['project_registry_unavailable'],
    discovery_fingerprint: `sha256:${'d'.repeat(64)}`,
  },
  meta: { ...metaOk, capabilities: { 'projectRegistry.write': false } },
}

export const discoveryExactMatchPayload = {
  data: {
    ...discoveryGitPayload.data,
    exact_match: { project_id: REG_BETA, slug: 'beta', display_name: 'Beta 项目' },
    discovery_fingerprint: `sha256:${'e'.repeat(64)}`,
  },
  meta: metaOk,
}

export const discoveryPossiblePayload = {
  data: {
    ...discoveryGitPayload.data,
    possible_projects: [{ project_id: REG_BETA, slug: 'beta', display_name: 'Beta 项目' }],
    discovery_fingerprint: `sha256:${'f'.repeat(64)}`,
  },
  meta: metaOk,
}

export const registerCreatedPayload = {
  data: { project_id: `prj_${'e5'.repeat(16)}`, slug: 'alpha' },
  meta: metaOk,
}

/** B3：server 权威开启 projectRegistry.write 的列表载荷（writable 变体） */
export const registryProjectsWritablePayload = {
  data: registryProjectsPayload.data,
  meta: { ...metaOk, capabilities: { 'projectRegistry.write': true } },
}

export const registryProjectsEmptyWritablePayload = {
  data: registryProjectsEmptyPayload.data,
  meta: { ...metaOk, capabilities: { 'projectRegistry.write': true } },
}
