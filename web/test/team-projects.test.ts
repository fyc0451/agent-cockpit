import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createTeamProject, listTeamProjects } from '../api/teamAuth'

describe('listTeamProjects', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('从 Hub /api/team/projects 抽出已加入 topic', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          projects: [
            { slug: 'proj-a', name: '项目 A', id: 1 },
            { project: { slug: 'proj-b', name: '项目 B', id: 2 } },
          ],
        }),
      })),
    )
    await expect(listTeamProjects()).resolves.toEqual([
      { slug: 'proj-a', name: '项目 A', id: 1 },
      { slug: 'proj-b', name: '项目 B', id: 2 },
    ])
  })

  it('创建 topic 只打 /api/team/projects，不选本机目录', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('/api/team/projects')
      expect(init?.method).toBe('POST')
      const body = JSON.parse(String(init?.body))
      expect(body.name).toBe('销售跟进')
      expect(body.slug).toMatch(/^[a-z0-9-]+$/)
      expect(body.mention_handle).toBe('fyc')
      expect(JSON.stringify(body)).not.toMatch(/home|path|cwd/)
      return {
        ok: true,
        status: 201,
        json: async () => ({ id: 9, slug: body.slug, name: body.name }),
      }
    })
    vi.stubGlobal('fetch', fetchMock)
    const topic = await createTeamProject('销售跟进', 'fyc')
    expect(topic.name).toBe('销售跟进')
    expect(topic.slug).toMatch(/^[a-z0-9-]+$/)
  })
})
