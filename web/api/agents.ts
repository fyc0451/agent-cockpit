// Workspace Agent 工作页客户端（最小 Agent loop）。
// 合同（ordinary frontend）：
// - POST /api/projects/{projectId}/workspaces/{workspaceId}/agents body 精确 {kind} + Idempotency-Key
// - GET  .../agents/{agentId}
// - POST .../agents/{agentId}/prompts body 精确 {prompt} + Idempotency-Key
// - data 精确 {agent_id, project_id, workspace_id, kind, status, transcript}
// 红线与 terminals.ts 相同：浏览器只提交 kind/prompt 与幂等键；绝不提交 workdir、
// command、session、pane、PID、env 或任何内部 ID。envelope/键集 fail-closed。

import { ApiError, ProtocolError } from './client'

export const AGENTS_API = {
  agents: (projectId: string, workspaceId: string) =>
    `/api/projects/${encodeURIComponent(projectId)}/workspaces/${encodeURIComponent(workspaceId)}/agents`,
  agent: (projectId: string, workspaceId: string, agentId: string) =>
    `${AGENTS_API.agents(projectId, workspaceId)}/${encodeURIComponent(agentId)}`,
  prompts: (projectId: string, workspaceId: string, agentId: string) =>
    `${AGENTS_API.agent(projectId, workspaceId, agentId)}/prompts`,
} as const

/** status 闭集（backend R3 冻结）：unknown 仅代表 live 存在但状态无法分类；live 缺失走 agent_not_found 404 */
export const AGENT_STATUSES = ['idle', 'working', 'blocked', 'done', 'unknown'] as const
export type AgentStatus = (typeof AGENT_STATUSES)[number]

/** data 精确六键；kind 非空字符串（可提交闭集见 SUPPORTED_AGENT_KINDS） */
export interface AgentView {
  agent_id: string
  project_id: string
  workspace_id: string
  kind: string
  status: AgentStatus
  transcript: string
}

/**
 * 本轮后端 ALLOWED_KINDS 白名单（合同）：可提交 kind 闭集。
 * env-check 已安装但不在白名单的类型（如 qodercli）不得进入可提交选项，避免 400。
 */
export const SUPPORTED_AGENT_KINDS = ['codex', 'claude', 'kimi', 'opencode', 'grok'] as const

// ---------- 守卫（fail-closed，同 terminals/localSlice 纪律） ----------

function fail(field: string): never {
  throw new ProtocolError(`agent 响应必填字段缺失或类型错误：${field}`)
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function reqString(v: unknown, field: string): string {
  if (typeof v !== 'string' || v === '') fail(field)
  return v
}

function reqEnum<T extends string>(v: unknown, field: string, allowed: readonly T[]): T {
  if (typeof v !== 'string' || !allowed.includes(v as T)) fail(field)
  return v as T
}

const AGENT_KEYS = ['agent_id', 'project_id', 'workspace_id', 'kind', 'status', 'transcript'] as const

export function assertAgentView(raw: unknown): AgentView {
  const o = isObj(raw) ? raw : fail('agent')
  const actual: string[] = Object.keys(o).sort()
  const expected: string[] = [...AGENT_KEYS].sort()
  if (actual.length !== expected.length || actual.some((k, i) => k !== expected[i])) {
    const missing = expected.filter((k) => !actual.includes(k))
    const extra = actual.filter((k) => !expected.includes(k))
    fail(`agent 键集（缺:${missing.join(',') || '无'} 多:${extra.join(',') || '无'}）`)
  }
  if (typeof o.transcript !== 'string') fail('agent.transcript')
  return {
    agent_id: reqString(o.agent_id, 'agent.agent_id'),
    project_id: reqString(o.project_id, 'agent.project_id'),
    workspace_id: reqString(o.workspace_id, 'agent.workspace_id'),
    kind: reqString(o.kind, 'agent.kind'),
    status: reqEnum(o.status, 'agent.status', AGENT_STATUSES),
    transcript: o.transcript,
  }
}

// ---------- G3 请求（精确 {data,meta} 顶层；Idempotency-Key 头） ----------

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    retryable?: boolean
    request_id?: string
    details?: unknown
  } | null
}

function codeForStatus(status: number): string {
  if (status === 400) return 'invalid_argument'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status >= 500) return 'server_error'
  return 'http_error'
}

async function request<T>(
  method: string,
  path: string,
  body?: Record<string, unknown>,
  idempotencyKey?: string,
): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(idempotencyKey !== undefined ? { 'Idempotency-Key': idempotencyKey } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
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

  if (isObj(parsed) && (parsed as ErrorEnvelope).error) {
    const e = (parsed as ErrorEnvelope).error!
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
    throw new ApiError({
      code: codeForStatus(res.status),
      message: `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
      status: res.status,
    })
  }
  if (!jsonOk) throw new ProtocolError('响应不是合法 JSON', { status: res.status })
  if (!isObj(parsed)) throw new ProtocolError('响应不是 G3 envelope（裸 body 不允许透传）', { status: res.status })
  if (!('data' in parsed)) throw new ProtocolError('响应 envelope 缺少 data 键', { status: res.status })
  if (!('meta' in parsed)) throw new ProtocolError('响应 envelope 缺少 meta 键', { status: res.status })
  const topKeys = Object.keys(parsed).sort()
  if (topKeys.length !== 2 || topKeys[0] !== 'data' || topKeys[1] !== 'meta') {
    throw new ProtocolError('响应 envelope 顶层键集必须是精确 {data,meta}', { status: res.status })
  }
  if (!isObj((parsed as Record<string, unknown>).meta)) {
    throw new ProtocolError('响应 envelope meta 必须是对象', { status: res.status })
  }
  return (parsed as { data: T }).data
}

// ---------- 端点 ----------

/** 启动（或幂等接管）一个 Agent 会话：body 精确 {kind} */
export function createAgent(
  projectId: string,
  workspaceId: string,
  kind: string,
  idempotencyKey: string,
): Promise<AgentView> {
  return request<unknown>('POST', AGENTS_API.agents(projectId, workspaceId), { kind }, idempotencyKey).then(
    (raw) => assertAgentView(raw),
  )
}

export function getAgent(ids: {
  projectId: string
  workspaceId: string
  agentId: string
}): Promise<AgentView> {
  return request<unknown>('GET', AGENTS_API.agent(ids.projectId, ids.workspaceId, ids.agentId)).then(
    (raw) => assertAgentView(raw),
  )
}

/** 发送任务：body 精确 {prompt} */
export function sendAgentPrompt(
  ids: { projectId: string; workspaceId: string; agentId: string },
  prompt: string,
  idempotencyKey: string,
): Promise<AgentView> {
  return request<unknown>(
    'POST',
    AGENTS_API.prompts(ids.projectId, ids.workspaceId, ids.agentId),
    { prompt },
    idempotencyKey,
  ).then((raw) => assertAgentView(raw))
}
