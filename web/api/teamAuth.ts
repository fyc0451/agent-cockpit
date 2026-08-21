// 团队区 API 客户端

import { ApiError } from './client'
import type { TeamTopic, TeamBinding, TeamSessionCandidate } from '../features/team/model'

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export async function teamLogin(username: string, password: string): Promise<void> {
  const response = await fetch('/api/team-auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
    credentials: 'include',
  })

  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'auth_failed',
      message: text || '登录失败',
      retryable: false,
    })
  }
}

export async function teamLogout(): Promise<void> {
  const response = await fetch('/api/team-auth/logout', {
    method: 'POST',
    credentials: 'include',
  })

  if (!response.ok) {
    throw new ApiError({
      code: 'logout_failed',
      message: '退出登录失败',
      retryable: false,
    })
  }
}

export async function teamAuthStatus(): Promise<{
  logged_in: boolean
  username: string | null
}> {
  const response = await fetch('/api/team-auth/status', {
    credentials: 'include',
  })

  if (!response.ok) {
    throw new ApiError({
      code: 'status_failed',
      message: '获取登录状态失败',
      retryable: true,
    })
  }

  const data = await response.json()
  if (!isObj(data)) {
    throw new ApiError({
      code: 'protocol_error',
      message: '状态响应格式错误',
      retryable: false,
    })
  }

  return {
    logged_in: data.logged_in === true,
    username: typeof data.username === 'string' ? data.username : null,
  }
}

export async function teamSessionBindings(): Promise<{
  sessions: TeamSessionCandidate[]
  bindings: TeamBinding[]
  topics: TeamTopic[]
}> {
  const response = await fetch('/api/team-auth/session-bindings', {
    credentials: 'include',
  })

  if (!response.ok) {
    throw new ApiError({
      code: 'bindings_failed',
      message: '获取绑定信息失败',
      retryable: true,
    })
  }

  const data = await response.json()
  if (!isObj(data)) {
    throw new ApiError({
      code: 'protocol_error',
      message: '绑定响应格式错误',
      retryable: false,
    })
  }

  const sessions: TeamSessionCandidate[] = []
  if (Array.isArray(data.sessions)) {
    for (const item of data.sessions) {
      if (!isObj(item)) continue
      const name = typeof item.name === 'string' ? item.name : ''
      const label = typeof item.label === 'string' ? item.label : name
      const generation = typeof item.generation === 'number' ? item.generation : 0
      if (name) {
        sessions.push({ name, label, generation })
      }
    }
  }

  const bindings: TeamBinding[] = []
  if (Array.isArray(data.bindings)) {
    for (const item of data.bindings) {
      if (!isObj(item)) continue
      const project_slug = typeof item.project_slug === 'string' ? item.project_slug : ''
      const session = typeof item.session === 'string' ? item.session : ''
      if (project_slug && session) {
        bindings.push({ project_slug, session })
      }
    }
  }

  // 从绑定中提取 topics（因为后端返回的 bindings 包含 project 信息）
  const topics: TeamTopic[] = []
  if (Array.isArray(data.bindings)) {
    for (const item of data.bindings) {
      if (!isObj(item)) continue
      const slug = typeof item.project_slug === 'string' ? item.project_slug : ''
      const name = typeof item.project_name === 'string' ? item.project_name : slug
      const id = typeof item.project_id === 'number' ? item.project_id : 0
      if (slug && !topics.find((t) => t.slug === slug)) {
        topics.push({ slug, name, id })
      }
    }
  }

  return { sessions, bindings, topics }
}

function parseTopic(raw: unknown): TeamTopic | null {
  if (!isObj(raw)) return null
  const nested = isObj(raw.project) ? raw.project : raw
  const slug =
    typeof nested.slug === 'string'
      ? nested.slug
      : typeof nested.project_slug === 'string'
        ? nested.project_slug
        : ''
  if (!slug) return null
  const name =
    typeof nested.name === 'string'
      ? nested.name
      : typeof nested.project_name === 'string'
        ? nested.project_name
        : slug
  const id =
    typeof nested.id === 'number'
      ? nested.id
      : typeof nested.project_id === 'number'
        ? nested.project_id
        : 0
  return { slug, name, id }
}

export async function listTeamProjects(): Promise<TeamTopic[]> {
  const response = await fetch('/api/team/projects', { credentials: 'include' })
  if (!response.ok) {
    throw new ApiError({
      code: 'projects_failed',
      message: '读取团队话题失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = Array.isArray(data)
    ? data
    : isObj(data) && Array.isArray(data.projects)
      ? data.projects
      : isObj(data) && Array.isArray(data.items)
        ? data.items
        : []
  const topics: TeamTopic[] = []
  for (const item of rows) {
    const topic = parseTopic(item)
    if (topic && !topics.find((row) => row.slug === topic.slug)) {
      topics.push(topic)
    }
  }
  return topics
}

function slugFromName(name: string): string {
  const base = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  const suffix = Date.now().toString(36)
  return (base ? `${base}-${suffix}` : `topic-${suffix}`).slice(0, 128)
}

function handleFromUsername(username: string): string {
  const handle = username
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 128)
  return handle || 'human'
}

export async function createTeamProject(
  name: string,
  username: string,
): Promise<TeamTopic> {
  const trimmed = name.trim()
  if (!trimmed) {
    throw new ApiError({
      code: 'invalid_name',
      message: 'topic 名字不能为空',
      retryable: false,
    })
  }
  const response = await fetch('/api/team/projects', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: trimmed,
      slug: slugFromName(trimmed),
      mention_handle: handleFromUsername(username),
    }),
  })
  if (!response.ok) {
    throw new ApiError({
      code: response.status === 403 ? 'forbidden' : 'create_failed',
      message:
        response.status === 403
          ? '只有管理员可以创建 topic'
          : await response.text() || '创建 topic 失败',
      retryable: false,
      status: response.status,
    })
  }
  const data = await response.json()
  const topic = parseTopic(data)
  if (!topic) {
    throw new ApiError({
      code: 'protocol_error',
      message: '创建 topic 响应格式错误',
      retryable: false,
    })
  }
  return topic
}

export async function teamBindSession(
  projectSlug: string,
  sessionName: string,
  replace = false,
): Promise<void> {
  const response = await fetch(`/api/team-auth/session-bindings/${encodeURIComponent(projectSlug)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session: sessionName, replace }),
    credentials: 'include',
  })

  if (!response.ok) {
    const text = await response.text()
    if (response.status === 409) {
      throw new ApiError({
        code: 'conflict',
        message: 'Session 或项目已有绑定',
        retryable: false,
      })
    }
    throw new ApiError({
      code: 'bind_failed',
      message: text || '绑定失败',
      retryable: false,
    })
  }
}
