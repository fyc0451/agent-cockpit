import { afterEach, describe, expect, it, vi } from 'vitest'
import { listTeamProjects, requestTeamJoin, teamRegister } from '../api/teamAuth'

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
})
