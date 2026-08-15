import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { metaOk, REG_P1 } from '../fixtures/api'
import { renderApp, stubDefaultFetch } from './helpers'

/**
 * 用户验收增强 · 壳层层级（冻结需求，base 上产品尚未实现）：
 * 进入 Workspace 后左侧保持项目层级；「项目」不得作为跳到 /projects 并清空上下文的主链接；
 * 必须有真实「管理项目 / 添加项目」入口；工作 / 文件 / 终端可见。
 */

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }
const HOME = '/projects/p1/workspaces/w1'

beforeEach(() => {
  window.localStorage.clear()
  stubDefaultFetch({ [WORK_ITEMS]: emptyWorkItems })
})

function rail() {
  return screen.getByRole('navigation', { name: '主导航' })
}

describe('验收 v2 · 项目层级与入口', () => {
  it('进入 Workspace 后「项目」不是跳到 /projects 并清空上下文的主链接', async () => {
    const user = userEvent.setup()
    renderApp(HOME)
    expect(await screen.findByLabelText('今天想推进什么？')).toBeInTheDocument()

    const nav = rail()
    const projectJump = within(nav)
      .queryAllByRole('link')
      .find((el) => el.getAttribute('title') === '项目' || /^项目$/.test(el.textContent ?? ''))
    if (projectJump) {
      expect(projectJump).not.toHaveAttribute('href', '/projects')
    }

    await user.click(screen.getByTitle('切换工作空间'))
    expect(await screen.findByRole('dialog', { name: '工作空间切换' })).toBeInTheDocument()
    expect(screen.getByTitle('切换项目')).toHaveTextContent('Project One')
    expect(screen.getByTitle('切换工作空间')).toHaveTextContent('本机工作区')
  })

  it('当前项目、Workspace、工作/文件/终端层级可见', async () => {
    renderApp(HOME)
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

  it('Workspace 内存在真实「管理项目」或「添加项目」入口，而不是只靠清空上下文的项目主链', async () => {
    renderApp(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const manage =
      screen.queryByRole('button', { name: '管理项目' }) ??
      screen.queryByRole('link', { name: '管理项目' }) ??
      screen.queryByRole('button', { name: '添加项目' }) ??
      screen.queryByRole('link', { name: '添加项目' })
    expect(manage, 'Workspace 内必须能找到管理项目或添加项目').not.toBeNull()
    if (manage?.tagName === 'A') {
      expect(manage).not.toHaveAttribute('href', '/projects')
    }
  })

  it('390 视口下层级与文件/终端入口仍在，结构不依赖复制生产 CSS', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
    window.dispatchEvent(new Event('resize'))
    renderApp(HOME)
    await screen.findByLabelText('今天想推进什么？')
    const nav = rail()
    await waitFor(() => expect(within(nav).getByTitle('文件')).toBeInTheDocument())
    expect(within(nav).getByTitle('工作对话')).toBeInTheDocument()
    expect(within(nav).getByTitle('终端')).toBeInTheDocument()
    expect(within(nav).getByTitle('文件')).toHaveAttribute('href', `${HOME}/files`)
    expect(within(nav).getByTitle('终端')).toHaveAttribute('href', `${HOME}/terminal`)
    expect(screen.getByRole('button', { name: '保存工作' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /开始任务|正在执行|已完成/ })).toBeNull()
  })
})
