import { describe, it, expect, vi, beforeEach } from 'vitest'
import { listTeamProjects } from '../api/teamAuth'

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
})
