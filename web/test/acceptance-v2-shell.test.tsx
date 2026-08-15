import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../app/App'
import { metaOk, REG_P1 } from '../fixtures/api'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider } from '../state/selection'
import { ThemeProvider } from '../state/theme'
import { stubDefaultFetch } from './helpers'

/**
 * 用户验收增强 · 壳层层级（冻结需求，对齐 550f9e9）：
 * 点击主导航「项目」主项不得改 URL、不得清空 Workspace/任务上下文。
 * 「管理项目」进项目页。「添加项目」显式导航到 /projects?wizard=1 再开向导；
 * 二者都不是项目主项，添加后不得再要求 Workspace 切换器仍在。
 * 工作/文件/终端必须挂在当前 Workspace 的 .rail-tree-workspace 内；
 * 不得再用「当前工作空间」重复标题或 rail 根上的 mobile title 当证据。
 * 本文件只做语义断言；390/bbox 由独立 Playwright 证明。
 */

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
const HOME = '/projects/p1/workspaces/w1'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>
}

function renderShell(initialRoute: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[initialRoute]}>
          <CapabilitiesProvider>
            <SelectionProvider>
              <LocationProbe />
              <App />
            </SelectionProvider>
          </CapabilitiesProvider>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  stubDefaultFetch({ [WORK_ITEMS]: emptyWorkItems })
})

function rail() {
  return screen.getByRole('navigation', { name: '主导航' })
}

function currentWorkspaceNode() {
  const nodes = [...rail().querySelectorAll('.rail-tree-workspace')] as HTMLElement[]
  expect(nodes.length, '必须存在 .rail-tree-workspace').toBeGreaterThan(0)
  const current = nodes.find((node) =>
    Boolean(within(node).queryByRole('link', { name: '本机工作区', exact: true })),
  )
  expect(current, '当前 Workspace「本机工作区」必须位于 .rail-tree-workspace').toBeTruthy()
  return current as HTMLElement
}

function expectWorkspaceFunctionLinks(ws: HTMLElement) {
  const work = within(ws).getByRole('link', { name: '工作', exact: true })
  const files = within(ws).getByRole('link', { name: '文件', exact: true })
  const terminal = within(ws).getByRole('link', { name: '终端', exact: true })
  expect(work).toBeVisible()
  expect(files).toBeVisible()
  expect(terminal).toBeVisible()
  expect(work).toHaveAttribute('href', HOME)
  expect(files).toHaveAttribute('href', `${HOME}/files`)
  expect(terminal).toHaveAttribute('href', `${HOME}/terminal`)
  expect(work.closest('.rail-tree-workspace')).toBe(ws)
  expect(files.closest('.rail-tree-workspace')).toBe(ws)
  expect(terminal.closest('.rail-tree-workspace')).toBe(ws)
}

describe('验收 v2 · 项目层级与入口', () => {
  it('点击主导航「项目」主项后精确 URL 不变，Workspace 与任务上下文仍在', async () => {
    const user = userEvent.setup()
    renderShell(HOME)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent(HOME)

    await user.click(within(rail()).getByTitle('项目'))

    expect(screen.getByTestId('location')).toHaveTextContent(HOME)
    expect(screen.queryByTestId('location')).not.toHaveTextContent('/projects?')
    expect(screen.getByTestId('location').textContent).toBe(HOME)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()
    expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('本机工作区')
    expectWorkspaceFunctionLinks(currentWorkspaceNode())
  })

  it('当前项目、Workspace、工作/文件/终端层级可见', async () => {
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const nav = rail()
    expect(within(nav).getByText('当前项目')).toBeInTheDocument()
    expect(within(nav).getByText('Project One')).toBeInTheDocument()
    const ws = currentWorkspaceNode()
    expect(within(ws).getByRole('link', { name: '本机工作区', exact: true })).toBeVisible()
    expectWorkspaceFunctionLinks(ws)
    expect(within(nav).queryByTitle('Agent')).toBeNull()
  })

  it('点击「管理项目」进入项目页，且该入口不是主导航「项目」主项', async () => {
    const user = userEvent.setup()
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const primary = within(rail()).getByTitle('项目')
    const manage =
      screen.queryByRole('button', { name: '管理项目' }) ??
      screen.queryByRole('link', { name: '管理项目' })
    expect(manage, '必须存在「管理项目」').not.toBeNull()
    expect(manage).not.toBe(primary)
    await user.click(manage)
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.getByTestId('location').textContent).toMatch(/^\/projects\/?$/)
    expect(screen.queryByTitle('切换工作空间')).not.toBeInTheDocument()
  })

  it('点击「添加项目」显式导航到 /projects?wizard=1 并打开真实向导，且不是项目主项', async () => {
    const user = userEvent.setup()
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const primary = within(rail()).getByTitle('项目')
    const add =
      screen.queryByRole('button', { name: '添加项目' }) ??
      screen.queryByRole('link', { name: '添加项目' })
    expect(add, '必须存在「添加项目」').not.toBeNull()
    expect(add).not.toBe(primary)
    await user.click(add)
    expect(await screen.findByRole('dialog', { name: '添加项目' })).toBeVisible()
    expect(screen.getByTestId('location').textContent).toBe('/projects?wizard=1')
  })

  it('文件/终端入口与无 Agent 语义仍在（尺寸由独立 Playwright 证明）', async () => {
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    expectWorkspaceFunctionLinks(currentWorkspaceNode())
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /开始任务|正在执行|已完成/ })).toBeNull()
  })
})
