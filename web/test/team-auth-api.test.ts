import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  approveTeamUser,
  approveTeamReplyRequest,
  createTeamSession,
  createTeamInvitation,
  deleteTeamSession,
  listTeamMembers,
  listTeamReplyRequests,
  listTeamProjects,
  rejectTeamReplyRequest,
  requestTeamJoin,
  setTeamConsultTarget,
  setTeamReplyMode,
  sendTeamPresence,
  teamChangePassword,
  teamRegister,
  teamPresenceClientId,
  teamSessionBindings,
} from '../api/teamAuth'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('团队普通成员 API', () => {
  it('修改密码使用现有 Human 登录态且不请求退出', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', fetchMock)

    await teamChangePassword('new-password-1234')

    expect(fetchMock).toHaveBeenCalledWith('/api/team-auth/password', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_password: 'new-password-1234' }),
      credentials: 'include',
    })
  })

  it('注册请求发送邀请码并保留 pending 后续审批语义', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 201 })
    vi.stubGlobal('fetch', fetchMock)

    await teamRegister({
      username: 'alice',
      displayName: 'Alice Chen',
      password: 'password-1234',
      inviteCode: 'INVITE-123',
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/team-auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'alice',
        display_name: 'Alice Chen',
        password: 'password-1234',
        invite_code: 'INVITE-123',
      }),
      credentials: 'include',
    })
  })

  it('项目列表保留 active 与 invited membership 状态', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        projects: [
          {
            id: 1,
            slug: 'active-team',
            name: 'Active Team',
            membership: { role: 'member', status: 'active', mention_handle: 'alice' },
          },
          {
            id: 2,
            slug: 'pending-team',
            name: 'Pending Team',
            membership: { role: 'member', status: 'invited', mention_handle: 'alice-2' },
          },
        ],
      }),
    }))

    const projects = await listTeamProjects()

    expect(projects.map((project) => project.membership?.status)).toEqual(['active', 'invited'])
  })

  it('上报稳定客户心跳并解析成员在线状态', async () => {
    window.localStorage.clear()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ online: true }) })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          members: [{
            human_id: 7,
            display_name: 'Alice',
            mention_handle: 'alice',
            role: 'member',
            status: 'active',
            online: true,
            last_seen_at: '2026-08-24T14:00:00Z',
          }],
        }),
      })
    vi.stubGlobal('fetch', fetchMock)

    const clientId = teamPresenceClientId()
    expect(teamPresenceClientId()).toBe(clientId)
    await sendTeamPresence(true)
    await expect(listTeamMembers('ready')).resolves.toMatchObject([{
      mention_handle: 'alice',
      online: true,
      last_seen_at: '2026-08-24T14:00:00Z',
    }])
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/team/presence', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId, online: true }),
    })
  })

  it('重复加入申请返回幂等 200 时仍视为成功', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', fetchMock)

    await expect(requestTeamJoin('ready', 'alice')).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenCalledWith('/api/team/projects/ready/join-requests', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mention_handle: 'alice' }),
    })
  })

  it('项目邀请绑定目标 topic，一次批准走组合端点', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => ({
          invite_code: 'INVITE-CORE',
          project_slug: 'core',
          expires_at: 123,
        }),
      } as Response)
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ ok: true }) } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(createTeamInvitation('core')).resolves.toEqual({
      inviteCode: 'INVITE-CORE',
      projectSlug: 'core',
      expiresAt: 123,
    })
    await approveTeamUser('alice')

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/team-auth/invitations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expires_in: 86400, project_slug: 'core' }),
      credentials: 'include',
    })
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/team-auth/users/alice/approve-team', {
      method: 'POST',
      credentials: 'include',
    })
  })

  it('按后端真实契约解析可绑定 Session 及 Lead', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        sessions: [
          {
            session: 'hr-ready-team',
            status: 'idle',
            agent_count: 1,
            lead: { agent: 'codex', mail_name: 'GoldRiver', status: 'idle' },
            ready: true,
            reason: null,
            project_ref: 'project-ready',
          },
          {
            session: 'hr-ready-3',
            status: 'idle',
            agent_count: 1,
            lead: null,
            ready: false,
            reason: '工作目录不匹配',
          },
        ],
        bindings: [
          {
            project_slug: 'ready',
            project_name: 'hr-ready',
            project_id: 7,
            session: 'hr-ready',
            active: false,
            ready: false,
            reason: 'Session 已停止',
            reply_mode: 'auto',
            managed_runtime: true,
            context: {
              freshness: 'current',
              observed_at: '2026-08-25T10:20:30+00:00',
              sha: '97dfca2123456789',
              dirty: true,
              handoff_updated: '2026-08-25',
              fingerprint: 'f'.repeat(64),
            },
            lead: { agent: 'codex', mail_name: 'GoldRiver', status: 'working' },
            consult_target: {
              session: 'dev-ready',
              ready: true,
              reason: null,
              lead: { agent: 'codex', mail_name: 'SwiftAnchor', status: 'idle' },
            },
          },
        ],
        consult_targets: [{
          session: 'dev-ready',
          status: 'idle',
          lead: { agent: 'codex', mail_name: 'SwiftAnchor', status: 'idle' },
          project_ref: 'project-ready',
        }],
      }),
    }))

    await expect(teamSessionBindings()).resolves.toMatchObject({
      sessions: [
        {
          name: 'hr-ready-team',
          label: 'hr-ready-team · Lead GoldRiver',
          ready: true,
          leadName: 'GoldRiver',
          projectRef: 'project-ready',
        },
        {
          name: 'hr-ready-3',
          label: 'hr-ready-3 · 工作目录不匹配',
          ready: false,
        },
      ],
      bindings: [
        {
          project_slug: 'ready',
          session: 'hr-ready',
          active: false,
          ready: false,
          reason: 'Session 已停止',
          projectRef: null,
          replyMode: 'auto',
          managedRuntime: true,
          context: {
            freshness: 'current',
            observedAt: '2026-08-25T10:20:30+00:00',
            sha: '97dfca2123456789',
            dirty: true,
            handoffUpdated: '2026-08-25',
            fingerprint: 'f'.repeat(64),
          },
          lead: { agent: 'codex', mailName: 'GoldRiver', status: 'working' },
          consultTarget: {
            session: 'dev-ready',
            ready: true,
            lead: { agent: 'codex', mailName: 'SwiftAnchor', status: 'idle' },
          },
        },
      ],
      consultTargets: [{
        session: 'dev-ready',
        label: 'dev-ready · Lead SwiftAnchor',
        projectRef: 'project-ready',
      }],
    })
  })

  it('一键创建 Team Session 发送工作区、Lead 与回复模式', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => ({ session: 'team-ready-1' }),
    } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(createTeamSession('ready', {
      workspaceId: 'workspace-1',
      agent: 'codex',
      model: 'gpt-5',
      replyMode: 'confirm',
    })).resolves.toEqual({ session: 'team-ready-1' })

    expect(fetchMock).toHaveBeenCalledWith('/api/team-auth/session-bindings/ready/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workspace_id: 'workspace-1',
        agent: 'codex',
        model: 'gpt-5',
        reply_mode: 'confirm',
        replace: false,
      }),
      credentials: 'include',
    })
  })

  it('删除 Topic Agent 使用显式 runtime 删除语义', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await deleteTeamSession('ready')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/team-auth/session-bindings/ready?delete_runtime=true',
      { method: 'DELETE', credentials: 'include' },
    )
  })

  it('显式保存或关闭同项目普通开发 Lead 咨询目标', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await setTeamConsultTarget('ready', 'dev-ready')
    await setTeamConsultTarget('ready', null)

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/team-auth/session-bindings/ready/consult-target',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ session: 'dev-ready' }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/team-auth/session-bindings/ready/consult-target',
      expect.objectContaining({ body: JSON.stringify({ session: null }) }),
    )
  })

  it('回复模式切换与消息级预授权只调用受限 Cockpit 路由', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200 } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          requests: [{
            inbox_item_id: 31,
            message_id: 12,
            status: 'awaiting_confirmation',
            decision: null,
            decided_at: null,
          }],
        }),
      } as Response)
      .mockResolvedValueOnce({ ok: true, status: 201 } as Response)
      .mockResolvedValueOnce({ ok: true, status: 200 } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await setTeamReplyMode('ready', 'auto')
    await expect(listTeamReplyRequests('ready')).resolves.toMatchObject([
      { inboxItemId: 31, messageId: 12, status: 'awaiting_confirmation' },
    ])
    await approveTeamReplyRequest('ready', 31)
    await rejectTeamReplyRequest('ready', 32)

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/team-auth/session-bindings/ready/reply-mode',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ reply_mode: 'auto' }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/team/projects/ready/reply-requests',
      { credentials: 'include' },
    )
    expect(String(fetchMock.mock.calls[2][0])).toContain('/reply-requests/31/approve')
    expect(String(fetchMock.mock.calls[3][0])).toContain('/reply-requests/32/reject')
  })
})
