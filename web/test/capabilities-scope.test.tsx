import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate, type NavigateFunction } from 'react-router-dom'
import App from '../app/App'
import { defaultFetchMap, metaOk, projectP1 } from '../fixtures/api'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider } from '../state/selection'
import { ThemeProvider } from '../state/theme'
import { stubFetch } from './helpers'

const projectP2 = {
  slug: 'p2',
  name: 'Project Two',
  branch: 'main',
  workspaces: [{ id: 'w1', name: 'B 工作区', location: 'local', branch: 'dev' }],
}

describe('capabilities 按 scope keyed snapshot（P1-3）', () => {
  function renderWithNav(initialRoute: string) {
    let nav: NavigateFunction | null = null
    function NavCapture() {
      nav = useNavigate()
      return null
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const utils = render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[initialRoute]}>
            <CapabilitiesProvider>
              <SelectionProvider>
                <NavCapture />
                <App />
              </SelectionProvider>
            </CapabilitiesProvider>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>,
    )
    return { ...utils, nav: () => nav! }
  }

  it('Project A 的 terminal.pty=true 不泄漏到其 Workspace 或 Project B', async () => {
    stubFetch({
      ...defaultFetchMap(),
      // A project：server cap terminal.pty=true
      '/api/projects/p1': {
        data: projectP1,
        meta: { ...metaOk, capabilities: { 'terminal.pty': { available: true, reason: null } } },
      },
      // B project（同 workspaceId w1）：meta 不带 capabilities
      '/api/projects/p2': { data: projectP2, meta: metaOk },
    })
    const { nav } = renderWithNav('/projects/p1/workspaces/w1/terminal')

    await screen.findByText('PTY 未接通')
    expect(screen.queryByText(/已由服务端 capability 标记为可用/)).not.toBeInTheDocument()

    await act(async () => {
      nav()('/projects/p2/workspaces/w1/terminal')
    })

    // B loading 与解析完成后都不得显示 A 的 Project 值
    expect(screen.queryByText(/已由服务端 capability 标记为可用/)).not.toBeInTheDocument()

    // B 解析后：w:p2/w1 scope 无 server 值 → 静态 fail-closed
    await screen.findByText('B 工作区')
    await waitFor(() => {
      expect(screen.getByText('PTY 未接通')).toBeInTheDocument()
    })
    for (const name of ['中断', '重连', '重启']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
    }
  })

  it('B 404：Workspace scope 无 server 值且 project 不存在 → typed error', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/projects/p1': {
        data: projectP1,
        meta: { ...metaOk, capabilities: { 'terminal.pty': { available: true, reason: null } } },
      },
      // p9 未配置 → 404 envelope
    })
    const { nav } = renderWithNav('/projects/p1/workspaces/w1/terminal')
    await screen.findByText('PTY 未接通')

    await act(async () => {
      nav()('/projects/p9/workspaces/w1/terminal')
    })
    // 404 typed error，且 A 的值不再出现
    await waitFor(() => {
      expect(screen.getByText('项目不存在')).toBeInTheDocument()
    })
    expect(screen.queryByText(/已由服务端 capability 标记为可用/)).not.toBeInTheDocument()
  })

  it('server 未声明的 key 在该 scope 保持 fail-closed', async () => {
    stubFetch({
      ...defaultFetchMap(),
      '/api/projects/p1': {
        data: projectP1,
        meta: { ...metaOk, capabilities: { 'terminal.pty': { available: true, reason: null } } },
      },
    })
    const { container } = renderWithNav('/projects/p1/workspaces/w1')
    const del = await screen.findByRole('button', { name: '删除工作空间' })
    expect(del).toHaveAttribute('aria-disabled', 'true')
    const cards = Array.from(container.querySelectorAll('.card'))
    expect(cards.find((c) => c.textContent?.includes('编辑器'))).toHaveClass('card--disabled')
  })
})
