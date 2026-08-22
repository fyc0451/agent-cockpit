import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TeamAdminPage } from '../pages/TeamAdminPage'
import { stubFetch } from './helpers'

const ADMIN_STATUS = {
  authenticated: true,
  profile: { username: 'fyc', display_name: '付彦超', roles: ['writer', 'admin'], status: 'active' },
}
const MEMBER_STATUS = {
  authenticated: true,
  profile: { username: 'bob', display_name: 'Bob', roles: ['writer'], status: 'active' },
}

function renderPage(fetchMap: Record<string, unknown>) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const fetchMock = stubFetch(fetchMap)
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TeamAdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...utils, fetchMock }
}

describe('TeamAdminPage 团队管理页', () => {
  it('未登录时提示去群聊页登录', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 401,
      json: async () => ({ detail: '未登录' }),
      text: async () => '{"detail":"未登录"}',
    }) as Response))
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <TeamAdminPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/尚未登录团队账号/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '群聊页' })).toHaveAttribute('href', '/chat')
  })

  it('非管理员登录后提示无权限', async () => {
    renderPage({
      '/api/team-auth/status': MEMBER_STATUS,
      '/api/team/projects': { projects: [] },
    })
    expect(await screen.findByText(/仅系统管理员可进行账号与成员审批/)).toBeInTheDocument()
    expect(screen.queryByText('账号管理')).not.toBeInTheDocument()
  })

  it('管理员可批准 pending 账号', async () => {
    const { fetchMock } = renderPage({
      '/api/team-auth/status': ADMIN_STATUS,
      '/api/team/projects': { projects: [] },
      '/api/team-auth/users': {
        users: [
          { username: 'fyc', display_name: '付彦超', roles: ['writer', 'admin'], status: 'active' },
          {
            username: 'xieyumin', display_name: '谢玉敏', roles: ['writer'], status: 'pending',
            requested_project_slug: 'ready',
          },
        ],
      },
    })
    expect(await screen.findByText('账号管理')).toBeInTheDocument()
    expect(await screen.findByText(/谢玉敏/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByText('批准加入'))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).includes('/api/team-auth/users/xieyumin/approve-team') &&
          (init as RequestInit | undefined)?.method === 'POST',
      )
      expect(call).toBeTruthy()
    })
  })

  it('管理员可为 topic 生成邀请链接', async () => {
    renderPage({
      '/api/team-auth/status': ADMIN_STATUS,
      '/api/team/projects': { projects: [{ slug: 'ready', name: 'hr-ready', id: 1 }] },
      '/api/team-auth/users': { users: [] },
      '/api/team-auth/invitations': {
        invite_code: 'INVITE-XYZ', project_slug: 'ready', expires_at: 123,
      },
    })
    await screen.findByText('账号管理')
    await userEvent.setup().click(screen.getByText('生成团队邀请链接'))
    expect(await screen.findByText(/team_invite=INVITE-XYZ/)).toBeInTheDocument()
  })

  it('管理员可新建 topic，重名错误直接展示', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body =
        url.endsWith('/api/team-auth/status') ? ADMIN_STATUS
        : url.endsWith('/api/team-auth/users') ? { users: [] }
        : url.endsWith('/api/team/projects') && method === 'POST'
          ? { slug: 'sales-x1', name: JSON.parse(String(init?.body)).name, id: 9 }
        : url.endsWith('/api/team/projects') ? { projects: [] }
        : {}
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <TeamAdminPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByText('Topic 管理')
    await userEvent.setup().type(screen.getByLabelText('topic 名称'), '销售跟进')
    await userEvent.setup().click(screen.getByRole('button', { name: '新建 topic' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).endsWith('/api/team/projects') &&
          (init as RequestInit | undefined)?.method === 'POST',
      )
      expect(call).toBeTruthy()
      expect(JSON.parse(String((call![1] as RequestInit).body)).name).toBe('销售跟进')
    })
  })

  it('管理员可审批 topic 加入申请', async () => {
    const { fetchMock } = renderPage({
      '/api/team-auth/status': ADMIN_STATUS,
      '/api/team/projects': { projects: [{ slug: 'ready', name: 'hr-ready', id: 1 }] },
      '/api/team-auth/users': { users: [] },
      '/api/team/projects/ready/members': {
        members: [
          { human_id: 7, display_name: '谢玉敏', mention_handle: 'xieyumin', role: 'member', status: 'invited' },
        ],
      },
    })
    await userEvent.setup().click(await screen.findByText('成员管理'))
    expect(await screen.findByText(/谢玉敏/)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByText('批准'))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input).includes('/api/team/projects/ready/members/7') &&
          (init as RequestInit | undefined)?.method === 'PATCH',
      )
      expect(call).toBeTruthy()
      expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ status: 'active' })
    })
  })
})
