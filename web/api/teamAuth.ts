// 团队区 API 客户端

import { ApiError } from './client'
import type {
  TeamTopic,
  TeamBinding,
  TeamSessionCandidate,
  TeamUser,
  TeamMember,
  TeamReplyRequest,
} from '../features/team/model'

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const TEAM_PRESENCE_CLIENT_STORAGE_KEY = 'cockpit-team-presence-client-id'

export function teamPresenceClientId(): string {
  const existing = window.localStorage.getItem(TEAM_PRESENCE_CLIENT_STORAGE_KEY)
  if (existing) return existing
  const generated = globalThis.crypto?.randomUUID?.()
    ?? `cockpit-${Date.now()}-${Math.random().toString(36).slice(2)}`
  window.localStorage.setItem(TEAM_PRESENCE_CLIENT_STORAGE_KEY, generated)
  return generated
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

export async function teamRegister(input: {
  username: string
  displayName: string
  password: string
  inviteCode: string
}): Promise<void> {
  const response = await fetch('/api/team-auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: input.username,
      display_name: input.displayName,
      password: input.password,
      invite_code: input.inviteCode,
    }),
    credentials: 'include',
  })

  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'registration_failed',
      message: text || '注册失败，请检查邀请码和账号信息',
      retryable: false,
      status: response.status,
    })
  }
}

export async function teamLogout(): Promise<void> {
  const response = await fetch('/api/team-auth/logout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: teamPresenceClientId() }),
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

export async function sendTeamPresence(online: boolean): Promise<void> {
  const response = await fetch('/api/team/presence', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: teamPresenceClientId(), online }),
  })
  if (!response.ok) {
    throw new ApiError({
      code: 'presence_failed',
      message: '更新在线状态失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

export async function teamChangePassword(newPassword: string): Promise<void> {
  const response = await fetch('/api/team-auth/password', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
    credentials: 'include',
  })

  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'password_change_failed',
      message: text || '修改密码失败',
      retryable: false,
      status: response.status,
    })
  }
}

export async function teamAuthStatus(): Promise<{
  logged_in: boolean
  username: string | null
  roles: string[]
}> {
  const response = await fetch('/api/team-auth/status', {
    credentials: 'include',
  })

  // 401 在此处的语义是「未登录」，不是故障：必须作为正常数据返回，
  // 否则 react-query 会把查询打入 error 重试，登录后的 invalidate
  // 与在途重试竞争，界面表现为「登录成功却又弹回登录框」。
  if (response.status === 401) {
    return { logged_in: false, username: null, roles: [] }
  }
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

  // 后端契约是 authenticated + profile{username,...}；logged_in/顶层 username
  // 是早期前端假设的字段，兼容两种形态。
  const profile = isObj(data.profile) ? data.profile : null
  const username =
    typeof data.username === 'string'
      ? data.username
      : profile && typeof profile.username === 'string'
        ? profile.username
        : null
  const rawRoles = Array.isArray(data.roles)
    ? data.roles
    : profile && Array.isArray(profile.roles)
      ? profile.roles
      : []
  return {
    logged_in: data.logged_in === true || data.authenticated === true,
    username,
    roles: rawRoles.filter((r): r is string => typeof r === 'string'),
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
      const name = typeof item.session === 'string' ? item.session : ''
      const lead = isObj(item.lead) ? item.lead : null
      const leadName = lead && typeof lead.mail_name === 'string' ? lead.mail_name : null
      const status = typeof item.status === 'string' ? item.status : ''
      const agentCount = typeof item.agent_count === 'number' ? item.agent_count : 0
      const ready = item.ready === true
      const reason = typeof item.reason === 'string' ? item.reason : null
      const projectRef = typeof item.project_ref === 'string' ? item.project_ref : null
      const label = [name, leadName ? `Lead ${leadName}` : null, ready ? null : reason ?? '不可绑定']
        .filter(Boolean)
        .join(' · ')
      if (name) {
        sessions.push({ name, label, status, agentCount, ready, reason, leadName, projectRef })
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
        const lead = isObj(item.lead) ? item.lead : null
        bindings.push({
          project_slug,
          session,
          active: typeof item.active === 'boolean' ? item.active : undefined,
          ready: typeof item.ready === 'boolean' ? item.ready : undefined,
          reason: typeof item.reason === 'string' ? item.reason : null,
          projectRef: typeof item.project_ref === 'string' ? item.project_ref : null,
          replyMode: item.reply_mode === 'auto' ? 'auto' : 'confirm',
          automationActive:
            typeof item.automation_active === 'boolean' ? item.automation_active : undefined,
          managedRuntime: item.managed_runtime === true,
          lead: lead
            ? {
                agent: typeof lead.agent === 'string' ? lead.agent : null,
                mailName: typeof lead.mail_name === 'string' ? lead.mail_name : null,
                status: typeof lead.status === 'string' ? lead.status : null,
              }
            : null,
        })
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
  const membershipOwner = 'membership' in nested
    ? nested
    : 'membership' in raw
      ? raw
      : null
  if (membershipOwner === null) return { slug, name, id }
  const membershipRaw = isObj(membershipOwner.membership) ? membershipOwner.membership : null
  const membership = membershipRaw
    ? {
        role: typeof membershipRaw.role === 'string' ? membershipRaw.role : 'member',
        status: typeof membershipRaw.status === 'string' ? membershipRaw.status : '',
        mention_handle:
          typeof membershipRaw.mention_handle === 'string' ? membershipRaw.mention_handle : '',
      }
    : null
  return { slug, name, id, membership }
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

export async function requestTeamJoin(
  projectSlug: string,
  mentionHandle: string,
): Promise<void> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(projectSlug)}/join-requests`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mention_handle: mentionHandle }),
    },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: response.status === 409 ? 'join_conflict' : 'join_failed',
      message: text || (response.status === 409 ? '该 @花名已被使用或申请状态冲突' : '申请加入失败'),
      retryable: false,
      status: response.status,
    })
  }
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

export async function createTeamSession(
  projectSlug: string,
  input: {
    workspaceId: string
    agent: 'codex' | 'claude' | 'kimi' | 'grok'
    model?: string
    replyMode: 'confirm' | 'auto'
    replace?: boolean
  },
): Promise<{ session: string }> {
  const response = await fetch(
    `/api/team-auth/session-bindings/${encodeURIComponent(projectSlug)}/create`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workspace_id: input.workspaceId,
        agent: input.agent,
        model: input.model || null,
        reply_mode: input.replyMode,
        replace: input.replace === true,
      }),
      credentials: 'include',
    },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: response.status === 409 ? 'conflict' : 'team_session_create_failed',
      message: text || '创建 Team Session 失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  if (!isObj(data) || typeof data.session !== 'string' || !data.session) {
    throw new ApiError({
      code: 'protocol_error',
      message: '创建 Team Session 响应格式错误',
      retryable: false,
    })
  }
  return { session: data.session }
}

export async function deleteTeamSession(projectSlug: string): Promise<void> {
  const response = await fetch(
    `/api/team-auth/session-bindings/${encodeURIComponent(projectSlug)}?delete_runtime=true`,
    { method: 'DELETE', credentials: 'include' },
  )
  if (!response.ok) {
    let message = '删除 Topic Agent 失败'
    try {
      const data: unknown = await response.json()
      if (isObj(data) && typeof data.detail === 'string') message = data.detail
    } catch {
      // 非 JSON 错误沿用稳定的用户提示。
    }
    throw new ApiError({
      code: 'team_session_delete_failed',
      message,
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

export async function setTeamReplyMode(
  projectSlug: string,
  replyMode: 'confirm' | 'auto',
): Promise<void> {
  const response = await fetch(
    `/api/team-auth/session-bindings/${encodeURIComponent(projectSlug)}/reply-mode`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reply_mode: replyMode }),
      credentials: 'include',
    },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'reply_mode_failed',
      message: text || '切换回复模式失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

function parseReplyRequest(raw: unknown): TeamReplyRequest | null {
  if (
    !isObj(raw)
    || typeof raw.inbox_item_id !== 'number'
    || raw.inbox_item_id <= 0
    || typeof raw.message_id !== 'number'
    || raw.message_id <= 0
  ) return null
  const status = raw.status
  if (
    status !== 'awaiting_confirmation'
    && status !== 'queued'
    && status !== 'processing'
    && status !== 'replied'
    && status !== 'ignored'
  ) return null
  const decision = raw.decision
  if (
    decision !== null
    && decision !== 'approved'
    && decision !== 'auto'
    && decision !== 'ignored'
  ) return null
  return {
    inboxItemId: raw.inbox_item_id,
    messageId: raw.message_id,
    status,
    decision,
    decidedAt: typeof raw.decided_at === 'string' ? raw.decided_at : null,
  }
}

export async function listTeamReplyRequests(projectSlug: string): Promise<TeamReplyRequest[]> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(projectSlug)}/reply-requests`,
    { credentials: 'include' },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'reply_requests_failed',
      message: '读取消息回复状态失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = isObj(data) && Array.isArray(data.requests) ? data.requests : []
  return rows.map(parseReplyRequest).filter((item): item is TeamReplyRequest => item !== null)
}

async function decideTeamReplyRequest(
  projectSlug: string,
  inboxItemId: number,
  decision: 'approve' | 'reject',
): Promise<void> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(projectSlug)}/reply-requests/${inboxItemId}/${decision}`,
    { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: '{}' },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: `reply_request_${decision}_failed`,
      message: text || (decision === 'approve' ? '授权回复失败' : '忽略消息失败'),
      retryable: response.status >= 500,
      status: response.status,
    })
  }
}

export function approveTeamReplyRequest(projectSlug: string, inboxItemId: number): Promise<void> {
  return decideTeamReplyRequest(projectSlug, inboxItemId, 'approve')
}

export function rejectTeamReplyRequest(projectSlug: string, inboxItemId: number): Promise<void> {
  return decideTeamReplyRequest(projectSlug, inboxItemId, 'reject')
}

// ---------- 管理员：账号与成员管理 ----------

function parseTeamUser(raw: unknown): TeamUser | null {
  if (!isObj(raw)) return null
  const username = typeof raw.username === 'string' ? raw.username : ''
  if (!username) return null
  return {
    subject: typeof raw.subject === 'string' ? raw.subject : undefined,
    username,
    display_name: typeof raw.display_name === 'string' ? raw.display_name : username,
    roles: Array.isArray(raw.roles)
      ? raw.roles.filter((r): r is string => typeof r === 'string')
      : [],
    status: typeof raw.status === 'string' ? raw.status : 'pending',
    requested_project_slug:
      typeof raw.requested_project_slug === 'string' ? raw.requested_project_slug : null,
  }
}

/** 系统账号列表（仅全局 admin 可用） */
export async function listTeamUsers(): Promise<TeamUser[]> {
  const response = await fetch('/api/team-auth/users', { credentials: 'include' })
  if (!response.ok) {
    throw new ApiError({
      code: response.status === 403 ? 'forbidden' : 'users_failed',
      message: response.status === 403 ? '只有系统管理员可以查看账号' : '读取账号列表失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = isObj(data) && Array.isArray(data.users) ? data.users : []
  const users: TeamUser[] = []
  for (const row of rows) {
    const user = parseTeamUser(row)
    if (user) users.push(user)
  }
  return users
}

/** 批准 / 停用 / 恢复系统账号（仅全局 admin） */
export async function setTeamUserStatus(username: string, status: string): Promise<void> {
  const response = await fetch(`/api/team-auth/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
    credentials: 'include',
  })
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'user_status_failed',
      message: text || '更新账号状态失败',
      retryable: false,
      status: response.status,
    })
  }
}

/** 一次批准：先幂等创建团队成员，再激活 Human Auth 账号。 */
export async function approveTeamUser(username: string): Promise<void> {
  const response = await fetch(
    `/api/team-auth/users/${encodeURIComponent(username)}/approve-team`,
    { method: 'POST', credentials: 'include' },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'team_approval_failed',
      message: text || '批准团队成员失败',
      retryable: false,
      status: response.status,
    })
  }
}

/** 生成绑定目标 topic 的一次性邀请（24h 有效，仅全局 admin）。 */
export async function createTeamInvitation(projectSlug: string): Promise<{
  inviteCode: string
  projectSlug: string
  expiresAt: number
}> {
  const response = await fetch('/api/team-auth/invitations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expires_in: 86400, project_slug: projectSlug }),
    credentials: 'include',
  })
  if (!response.ok) {
    throw new ApiError({
      code: response.status === 403 ? 'forbidden' : 'invite_failed',
      message: response.status === 403 ? '只有系统管理员可以生成邀请码' : '生成邀请码失败',
      retryable: false,
      status: response.status,
    })
  }
  const data = await response.json()
  if (
    !isObj(data)
    || typeof data.invite_code !== 'string'
    || !data.invite_code
    || typeof data.project_slug !== 'string'
    || !data.project_slug
  ) {
    throw new ApiError({ code: 'protocol_error', message: '邀请码响应格式错误', retryable: false })
  }
  return {
    inviteCode: data.invite_code,
    projectSlug: data.project_slug,
    expiresAt: typeof data.expires_at === 'number' ? data.expires_at : 0,
  }
}

function parseTeamMember(raw: unknown): TeamMember | null {
  if (!isObj(raw)) return null
  const humanId = typeof raw.human_id === 'number' ? raw.human_id : 0
  if (!humanId) return null
  const agent = isObj(raw.agent) ? raw.agent : null
  return {
    human_id: humanId,
    display_name: typeof raw.display_name === 'string' ? raw.display_name : '',
    mention_handle: typeof raw.mention_handle === 'string' ? raw.mention_handle : '',
    role: typeof raw.role === 'string' ? raw.role : 'member',
    status: typeof raw.status === 'string' ? raw.status : '',
    online: raw.online === true,
    last_seen_at: typeof raw.last_seen_at === 'string' ? raw.last_seen_at : null,
    agent: agent
      ? {
          name: typeof agent.name === 'string' ? agent.name : null,
          kind: typeof agent.kind === 'string' ? agent.kind : null,
          status: typeof agent.status === 'string' ? agent.status : null,
          managed: agent.managed === true,
        }
      : null,
  }
}

/** topic 成员列表（含待审批的加入申请） */
export async function listTeamMembers(projectSlug: string): Promise<TeamMember[]> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(projectSlug)}/members`,
    { credentials: 'include' },
  )
  if (!response.ok) {
    throw new ApiError({
      code: 'members_failed',
      message: '读取成员列表失败',
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  const data = await response.json()
  const rows = isObj(data) && Array.isArray(data.members) ? data.members : []
  const members: TeamMember[] = []
  for (const row of rows) {
    const member = parseTeamMember(row)
    if (member) members.push(member)
  }
  return members
}

/** 审批加入申请 / 移除 / 恢复成员或调整角色（topic 管理员） */
export async function patchTeamMember(
  projectSlug: string,
  humanId: number,
  patch: { status?: string; role?: string },
): Promise<void> {
  const response = await fetch(
    `/api/team/projects/${encodeURIComponent(projectSlug)}/members/${humanId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
      credentials: 'include',
    },
  )
  if (!response.ok) {
    const text = await response.text()
    throw new ApiError({
      code: 'member_patch_failed',
      message: text || '更新成员失败',
      retryable: false,
      status: response.status,
    })
  }
}
