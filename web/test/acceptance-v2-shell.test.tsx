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
 * 用户验收增强 · 壳层层级（冻结需求）：
 * 点击主导航「项目」主项不得改 URL、不得清空 Workspace/任务上下文。
 * 「管理项目」进项目页，「添加项目」打开真实向导；二者都不是项目主项。
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
    expect(within(rail()).getByTitle('工作对话')).toBeInTheDocument()
    expect(within(rail()).getByTitle('文件')).toBeInTheDocument()
    expect(within(rail()).getByTitle('终端')).toBeInTheDocument()
  })

  it('当前项目、Workspace、工作/文件/终端层级可见', async () => {
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const nav = rail()
    expect(within(nav).getByText('当前项目')).toBeInTheDocument()
    expect(within(nav).getByText('Project One')).toBeInTheDocument()
    expect(within(nav).getByText('当前工作空间')).toBeInTheDocument()
    expect(within(nav).getAllByText('本机工作区').length).toBeGreaterThan(0)
    expect(within(nav).getByTitle('工作对话')).toBeInTheDocument()
    expect(within(nav).getByTitle('文件')).toHaveAttribute('href', `${HOME}/files`)
    expect(within(nav).getByTitle('终端')).toHaveAttribute('href', `${HOME}/terminal`)
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

  it('点击「添加项目」打开真实向导，且该入口不是主导航「项目」主项', async () => {
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
    expect(await screen.findByRole('dialog', { name: '添加项目' })).toBeInTheDocument()
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('本机工作区')
  })

  it('文件/终端入口与无 Agent 语义仍在（尺寸由独立 Playwright 证明）', async () => {
    renderShell(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const nav = rail()
    expect(within(nav).getByTitle('工作对话')).toBeInTheDocument()
    expect(within(nav).getByTitle('文件')).toHaveAttribute('href', `${HOME}/files`)
    expect(within(nav).getByTitle('终端')).toHaveAttribute('href', `${HOME}/terminal`)
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /开始任务|正在执行|已完成/ })).toBeNull()
  })
})
