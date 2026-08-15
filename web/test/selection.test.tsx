import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../app/App'
import { defaultFetchMap, metaOk, projectP1, REG_P1, REG_P2, workspaceP2W1 } from '../fixtures/api'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider, useSelection, type SelectionState } from '../state/selection'
import { ThemeProvider } from '../state/theme'
import { stubFetch } from './helpers'

/** 与 p1 的 w1 错开，避免旧 fixture 同名 ID 把跨项目串线伪装成合法切换 */
const P2_WS_ID = 'w-p2'
const p2Workspace = { ...workspaceP2W1, workspace_id: P2_WS_ID }
const projectP2 = { slug: 'p2', name: 'Project Two', branch: 'main', workspaces: [] }

type Snapshot = Pick<SelectionState, 'projectSlug' | 'workspaceId' | 'project' | 'workspace'>

describe('selection 原子清空（item 3）', () => {
  it('projectSlug 变化时不存在「新 project + 旧 workspace」的任何渲染帧', async () => {
    const snapshots: Snapshot[] = []
    function Probe() {
      const s = useSelection()
      snapshots.push({
        projectSlug: s.projectSlug,
        workspaceId: s.workspaceId,
        project: s.project,
        workspace: s.workspace,
      })
      return null
    }

    stubFetch({
      ...defaultFetchMap(),
      [`/api/projects/${REG_P1}/workspaces/w1/work-items`]: {
        data: { items: [], next_cursor: null },
        meta: metaOk,
      },
      [`/api/projects/${REG_P2}/workspaces/${P2_WS_ID}/work-items`]: {
        data: { items: [], next_cursor: null },
        meta: metaOk,
      },
      [`/api/project-registry/projects/${REG_P2}/workspaces`]: {
        data: { items: [p2Workspace] },
        meta: metaOk,
      },
      [`/api/project-registry/projects/${REG_P2}/workspaces/${P2_WS_ID}`]: {
        data: p2Workspace,
        meta: metaOk,
      },
      '/api/overview': {
        projects: [projectP1, projectP2],
        total_unread: 0,
        total_projects: 2,
        total_agents: 0,
        agent_mail: { available: true },
      },
      '/api/attention': { items: [], sessions: [], count: 0, mail_unread: 0, capabilities: {} },
      '/api/herdr/status': { available: true, binary: '/usr/local/bin/herdr' },
      '/api/settings': { language: 'zh', known_agents: ['claude'], languages: ['zh', 'en'] },
      '/api/projects/p2': { data: projectP2, meta: metaOk },
      '/api/projects/p2/workbench': { data: {}, meta: metaOk },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/projects/p1/workspaces/w1']}>
            <CapabilitiesProvider>
              <SelectionProvider>
                <Probe />
                <App />
              </SelectionProvider>
            </CapabilitiesProvider>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>,
    )

    await screen.findByText('本机工作区')
    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.workspace?.id).toBe('w1')
      expect(last.project?.project_id).toBe(REG_P1)
    })

    const user = userEvent.setup()
    await user.click(screen.getByTitle('切换项目'))
    await user.click(await screen.findByRole('button', { name: /Project Two/ }))

    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.projectSlug).toBe('p2')
    })

    for (const s of snapshots) {
      if (s.projectSlug === 'p2') {
        expect(s.project?.project_id ?? null).not.toBe(REG_P1)
        if (s.project) {
          expect(s.project.project_id).toBe(REG_P2)
          expect(s.project.slug).toBe('p2')
        }
        expect([s.project?.project_id, s.workspace?.id ?? s.workspaceId]).not.toEqual([REG_P1, 'w1'])
        if (s.workspace) {
          expect([s.project?.project_id, s.workspaceId]).toEqual([REG_P2, P2_WS_ID])
          expect(s.workspace.id).toBe(P2_WS_ID)
          expect(s.workspace.name).toBe('B 工作区')
        }
      }
    }

    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.project?.slug).toBe('p2')
      expect(last.project?.project_id).toBe(REG_P2)
      expect(last.workspaceId).toBe(P2_WS_ID)
      expect(last.workspace?.id).toBe(P2_WS_ID)
    })
  })
})
