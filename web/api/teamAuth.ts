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
