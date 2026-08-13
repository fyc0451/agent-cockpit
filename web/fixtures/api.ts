import type { Project } from '../api/types'

/** 测试用后端载荷（fixtures，不是演示数据——页面本身不消费这些） */

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

export const overviewPayload = {
  data: { projects: [projectP1] },
  meta: metaOk,
}

export const attentionPayload = {
  data: {
    items: [
      {
        id: 'a1',
        kind: 'review',
        title: 'ReviewPacket 待决定',
        summary: 'run r-9 的变更需要人工决定',
        status: 'needs-action',
        project: 'p1',
      },
      {
        id: 'a2',
        kind: 'note',
        title: '恢复提醒',
        summary: 'workspace w2 有可恢复会话',
        status: 'info',
        project: 'p1',
      },
    ],
  },
  meta: metaOk,
}

export const workbenchPayload = {
  data: {
    agents: [{ id: 'agent-1', name: 'kimi', status: 'running' }],
    tasks: [{ id: 't1', title: '修复登录回归', status: 'in_progress' }],
    activity: [{ title: 'kimi 完成了 t0' }],
  },
  meta: metaOk,
}

export const envCheckPayload = {
  data: {
    ok: false,
    checks: [
      { name: 'python', status: 'ok', ok: true, message: '3.12.7' },
      { name: 'herdr', status: 'fail', ok: false, message: 'Herdr 未运行' },
    ],
  },
  meta: metaOk,
}

export const settingsPayload = {
  data: { harness: { default: 'kimi' }, runtime: { mode: 'source' }, nodes: [] },
  meta: metaOk,
}

export const herdrStatusPayload = {
  data: { status: 'running', name: 'Herdr', healthy: true },
  meta: metaOk,
}

export const fileRootsPayload = {
  data: { roots: [{ id: 'root-1', name: 'repo', path: '/repos/p1', kind: 'git' }] },
  meta: metaOk,
}

export const fileSearchPayload = {
  data: { items: [{ name: 'README.md', path: '/repos/p1/README.md', kind: 'file' }] },
  meta: metaOk,
}

export const tasksPayload = {
  data: { items: [{ id: 't1', title: '修复登录回归', status: 'in_progress', kind: 'task' }] },
  meta: metaOk,
}

/** 默认路由 → 载荷映射，测试可在此基础上覆盖 */
export function defaultFetchMap(): Record<string, unknown> {
  return {
    '/api/overview': overviewPayload,
    '/api/attention': attentionPayload,
    '/api/projects/p1': { data: projectP1, meta: metaOk },
    '/api/projects/p1/workbench': workbenchPayload,
    '/api/env-check': envCheckPayload,
    '/api/settings': settingsPayload,
    '/api/herdr/status': herdrStatusPayload,
    '/api/files/roots': fileRootsPayload,
    '/api/tasks': tasksPayload,
    // WEB-003 registry / runtime-nodes（GET；POST discovery/register 不进 map，由测试按 method 分支）
    '/api/project-registry/projects': registryProjectsPayload,
    '/api/runtime-nodes': runtimeNodesPayload,
    '/api/runtime-nodes/local/roots': rootsPayload,
    '/api/runtime-nodes/local/directories': directoriesPayload,
  }
}

// ================= WEB-003：Project Registry / discovery / runtime-nodes =================
// 全部为完整 G3 envelope（data+meta+sources/capabilities）；字段形状照 api-freeze-v1 §2。

export const REG_ROOT_CODE = 'root_0123456789abcdef01234567'
export const REG_ROOT_DOCS = 'root_f0123456789abcde01234567'

export const registryProjectsPayload = {
  data: {
    items: [
      {
        project_id: `prj_${'a1'.repeat(16)}`,
        slug: 'alpha',
        display_name: 'Alpha 项目',
        goal: null,
        lifecycle: 'active',
        version: 1,
        created_at: '2026-08-01T00:00:00+00:00',
        updated_at: '2026-08-01T00:00:00+00:00',
        repo_locations: [
          {
            repo_location_id: `loc_${'b2'.repeat(16)}`,
            project_id: `prj_${'a1'.repeat(16)}`,
            node_id: 'local',
            canonical_path: '/repos/alpha',
            vcs_kind: 'git',
            availability: 'available',
            lifecycle: 'active',
            version: 1,
          },
        ],
      },
      {
        project_id: `prj_${'c3'.repeat(16)}`,
        slug: 'beta',
        display_name: 'Beta 项目',
        goal: 'beta goal',
        lifecycle: 'active',
        version: 3,
        created_at: '2026-08-02T00:00:00+00:00',
        updated_at: '2026-08-03T00:00:00+00:00',
        repo_locations: [
          {
            repo_location_id: `loc_${'d4'.repeat(16)}`,
            project_id: `prj_${'c3'.repeat(16)}`,
            node_id: 'local',
            canonical_path: '/repos/beta',
            vcs_kind: 'none',
            availability: 'offline',
            lifecycle: 'active',
            version: 1,
          },
        ],
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

export const rootsPayload = {
  data: {
    roots: [
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
          project_id: `prj_${'c3'.repeat(16)}`,
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
  meta: metaOk,
}

export const discoveryPlainPayload = {
  data: {
    ...discoveryGitPayload.data,
    locator: { node_id: 'local', root_id: REG_ROOT_CODE, path: 'beta' },
    display_path: '代码/beta',
    vcs: { ...vcsGit, kind: 'none', git_root_digest: null, repository_fingerprint: null, head: null, branch_present: false, refs_count: 0 },
    discovery_fingerprint: `sha256:${'b'.repeat(64)}`,
  },
  meta: metaOk,
}

export const discoveryDegradedPayload = {
  data: {
    ...discoveryGitPayload.data,
    complete: false,
    warnings: ['project_registry_unavailable'],
    discovery_fingerprint: `sha256:${'d'.repeat(64)}`,
  },
  meta: metaOk,
}

export const discoveryExactMatchPayload = {
  data: {
    ...discoveryGitPayload.data,
    exact_match: { project_id: `prj_${'c3'.repeat(16)}`, slug: 'beta', display_name: 'Beta 项目' },
    discovery_fingerprint: `sha256:${'e'.repeat(64)}`,
  },
  meta: metaOk,
}

export const discoveryPossiblePayload = {
  data: {
    ...discoveryGitPayload.data,
    possible_projects: [{ project_id: `prj_${'c3'.repeat(16)}`, slug: 'beta', display_name: 'Beta 项目' }],
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
