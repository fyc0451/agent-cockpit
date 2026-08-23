import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  approveTeamUser,
  approveTeamReplyDraft,
  createTeamInvitation,
  listTeamReplyDrafts,
  listTeamProjects,
  rejectTeamReplyDraft,
  requestTeamJoin,
  setTeamReplyMode,
  teamChangePassword,
  teamRegister,
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
          },
        ],
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
        },
      ],
    })
  })

  it('回复模式切换与草稿决策只调用受限 Cockpit 路由', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200 } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          drafts: [{
            id: 12,
            inbox_item_id: 31,
            subject: '回复',
            body_md: '已处理',
            importance: 'normal',
            mention_handles: ['alice'],
            status: 'pending',
            message_id: null,
            created_at: '2026-08-23 10:00:00',
            updated_at: '2026-08-23 10:00:00',
            decided_at: null,
          }],
        }),
      } as Response)
      .mockResolvedValueOnce({ ok: true, status: 201 } as Response)
      .mockResolvedValueOnce({ ok: true, status: 200 } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await setTeamReplyMode('ready', 'auto')
    await expect(listTeamReplyDrafts('ready')).resolves.toMatchObject([
      { id: 12, body: '已处理', mentionHandles: ['alice'], status: 'pending' },
    ])
    await approveTeamReplyDraft('ready', 12)
    await rejectTeamReplyDraft('ready', 13)

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
      '/api/team/projects/ready/reply-drafts',
      { credentials: 'include' },
    )
    expect(String(fetchMock.mock.calls[2][0])).toContain('/reply-drafts/12/approve')
    expect(String(fetchMock.mock.calls[3][0])).toContain('/reply-drafts/13/reject')
  })
})
