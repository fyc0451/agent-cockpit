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
  }
}
