import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../app/App'
import { defaultFetchMap, metaOk, projectP1 } from '../fixtures/api'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider, useSelection, type SelectionState } from '../state/selection'
import { ThemeProvider } from '../state/theme'
import { stubFetch } from './helpers'

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
      '/api/overview': { data: { projects: [projectP1, projectP2] }, meta: metaOk },
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

    // 等 p1/w1 selection 建立（rail 出现 workspace 名）
    await screen.findByText('本机工作区')
    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.workspace?.id).toBe('w1')
    })

    // 真实应用内跳转：Project drawer → p2 workbench
    const user = userEvent.setup()
    await user.click(screen.getByTitle('切换项目'))
    await user.click(await screen.findByRole('button', { name: /Project Two/ }))

    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.projectSlug).toBe('p2')
    })

    // 任意 p2 帧都不得携带旧 workspace（id 或对象）
    for (const s of snapshots) {
      if (s.projectSlug === 'p2') {
        expect(s.workspaceId).toBeNull()
        expect(s.workspace).toBeNull()
      }
    }
    // 且最终 project 也切换为 p2 的载荷
    await waitFor(() => {
      const last = snapshots[snapshots.length - 1]
      expect(last.project?.slug).toBe('p2')
    })
  })
})
