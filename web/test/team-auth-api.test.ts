import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  approveTeamUser,
  createTeamInvitation,
  listTeamProjects,
  requestTeamJoin,
  teamRegister,
  teamSessionBindings,
} from '../api/teamAuth'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('团队普通成员 API', () => {
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
        },
      ],
    })
  })
})
