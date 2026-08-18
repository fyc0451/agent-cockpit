import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import type { TerminalStreamHandlers, TerminalTicketView } from '../api/terminals'
import type { Project, Workspace } from '../api/types'
import {
  clampWorkspaceDimensions,
  TerminalPage,
  pickSplitPartner,
} from '../pages/TerminalPage'

const mocks = vi.hoisted(() => ({
  isTouch: false,
  tickets: [] as TerminalTicketView[],
  handlers: undefined as TerminalStreamHandlers | undefined,
}))

vi.mock('../features/terminal/touchScroll', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../features/terminal/touchScroll')>()
  return {
    ...actual,
    isTouchTerminal: () => mocks.isTouch,
    // 触摸滚动桥依赖真实 xterm 实例，本分屏用例替换为空操作
    enableTermTouchScroll: () => () => {},
    enableTermKeyboardGuard: () => () => {},
  }
})

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit() {}
    proposeDimensions() {
      return { cols: 80, rows: 24 }
    }
  },
}))

vi.mock('@xterm/addon-webgl', () => ({
  WebglAddon: class {
    onContextLoss() {}
    dispose() {}
  },
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80
    rows = 24
    loadAddon() {}
    open() {}
    write(_data: unknown, callback?: () => void) {
      callback?.()
    }
    reset() {}
    refresh() {}
    dispose() {}
    onData() {
      return { dispose() {} }
    }
    parser = {
      registerOscHandler() {
        return { dispose() {} }
      },
    }
  },
}))

function makeTicket(id: string): TerminalTicketView {
  return {
    ticket: {
      ticket_id: id,
      project_id: 'p1',
      workspace_id: 'w1',
      desired_state: 'running',
      observed_state: 'running',
      engine_generation: 1,
      reconnect_cursor: 0,
      receipt_refs: [],
      revision: 1,
      created_at: '2026-08-18T00:00:00Z',
      updated_at: '2026-08-18T00:00:00Z',
    },
    runtime: { state: 'running', replay_available: true, replay_truncated: false },
  }
}

vi.mock('../api/terminals', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/terminals')>()
  return {
    ...actual,
    listTerminalTickets: () => Promise.resolve({ items: mocks.tickets, next_cursor: null }),
    connectTerminalStream: (
      _ids: unknown,
      _fence: unknown,
      handlers: TerminalStreamHandlers,
    ) => {
      mocks.handlers = handlers
      return {
        sendInput: vi.fn(),
        sendResize: vi.fn(),
        close: vi.fn(),
        ready: true,
      }
    },
  }
})

// 页头两个 scope 组件内部走 registry/workspace 查询；分屏用例无关，直接喂固定身份
vi.mock('../features/ProjectScope', () => ({
  ProjectScope: ({ children }: { slug: string; children: (project: Project) => ReactNode }) => (
    <>{children({ slug: 'demo', name: 'Demo', project_id: 'p1' })}</>
  ),
}))

vi.mock('../features/WorkspaceScope', () => ({
  WorkspaceScope: ({
    children,
  }: {
    project: Project
    children: (workspace: Workspace) => ReactNode
  }) => <>{children({ id: 'w1', workspace_id: 'w1', name: 'ws', version: 1 })}</>,
}))

vi.mock('../state/capabilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../state/capabilities')>()
  return {
    ...actual,
    capability: () => ({ available: true, reason: null }),
    useCapability: () => ({ available: true, reason: null }),
  }
})

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects/demo/workspaces/w1/terminal']}>
      <TerminalPage />
    </MemoryRouter>,
  )
}

const surface = (id: string) => screen.getByTestId(`terminal-surface-${id}`)

describe('pickSplitPartner 配对纯函数', () => {
  it('保留仍有效的当前配对', () => {
    expect(pickSplitPartner(['t1', 't2', 't3'], 't1', 't3')).toBe('t3')
  })

  it('无当前配对时取 selected 的下一个（环形回绕）', () => {
    expect(pickSplitPartner(['t1', 't2', 't3'], 't1', null)).toBe('t2')
    expect(pickSplitPartner(['t1', 't2', 't3'], 't3', null)).toBe('t1')
  })

  it('当前配对失效（=selected 或已关闭）时重新配对', () => {
    expect(pickSplitPartner(['t1', 't2'], 't2', 't2')).toBe('t1')
    expect(pickSplitPartner(['t1', 't2'], 't1', 't9')).toBe('t2')
  })

  it('只有一个 open tab 或没有 selected 时返回 null', () => {
    expect(pickSplitPartner(['t1'], 't1', null)).toBeNull()
    expect(pickSplitPartner(['t1', 't2'], null, null)).toBeNull()
  })
})

describe('workspace terminal 大屏尺寸', () => {
  beforeEach(() => {
    mocks.isTouch = false
    mocks.tickets = []
    mocks.handlers = undefined
  })

  it('将超出 workspace PTY 上限的列数/行数夹回合法范围', () => {
    expect(clampWorkspaceDimensions({ cols: 516, rows: 84 })).toEqual({
      cols: 500,
      rows: 84,
    })
    expect(clampWorkspaceDimensions({ cols: 80, rows: 301 })).toEqual({
      cols: 80,
      rows: 300,
    })
  })

})

describe('TerminalPage 🧱 前端分屏', () => {
  beforeEach(() => {
    mocks.isTouch = false
    mocks.handlers = undefined
    mocks.tickets = [makeTicket('t1')]
  })

  it('桌面（非触屏）也渲染 🧱；只有 1 个 open tab 时禁用', async () => {
    renderPage()
    await screen.findByTestId('terminal-surface-t1')
    const toggle = screen.getByTestId('terminal-split-toggle')
    expect(toggle).toBeDisabled()
    expect(toggle).toHaveAttribute('title', '需要至少两个打开的终端标签页才能分屏')
  })

  it('两个 open tab 时选「左右」：容器带 is-split-h 且两个 surface 可见，「取消分屏」恢复单可见', async () => {
    mocks.tickets = [makeTicket('t1'), makeTicket('t2')]
    renderPage()
    await screen.findByTestId('terminal-surface-t1')

    // 第二个 tab 默认未接管（detached），点击标签打开并选中
    fireEvent.click(screen.getByText('终端 2'))
    await screen.findByTestId('terminal-surface-t2')
    expect(surface('t1')).toHaveClass('terminal-surface--hidden')
    expect(surface('t2')).not.toHaveClass('terminal-surface--hidden')

    const toggle = screen.getByTestId('terminal-split-toggle')
    expect(toggle).not.toBeDisabled()

    fireEvent.click(toggle)
    fireEvent.click(screen.getByText('⬌ 左右'))

    // 分屏容器出现，selected(t2) + 配对(t1) 两个 surface 同时可见
    const view = screen.getByTestId('terminal-split-view')
    expect(view).toHaveClass('is-split-h')
    expect(surface('t1')).not.toHaveClass('terminal-surface--hidden')
    expect(surface('t2')).not.toHaveClass('terminal-surface--hidden')
    // 操作行在选定后收起
    expect(screen.queryByTestId('terminal-split-actions')).not.toBeInTheDocument()

    // 取消分屏：恢复单可见
    fireEvent.click(toggle)
    fireEvent.click(screen.getByText('取消分屏'))
    expect(screen.queryByTestId('terminal-split-view')).not.toBeInTheDocument()
    expect(surface('t1')).toHaveClass('terminal-surface--hidden')
    expect(surface('t2')).not.toHaveClass('terminal-surface--hidden')
  })

  it('选「上下」：容器带 is-split-v', async () => {
    mocks.tickets = [makeTicket('t1'), makeTicket('t2')]
    renderPage()
    await screen.findByTestId('terminal-surface-t1')
    fireEvent.click(screen.getByText('终端 2'))
    await screen.findByTestId('terminal-surface-t2')

    fireEvent.click(screen.getByTestId('terminal-split-toggle'))
    fireEvent.click(screen.getByText('⬍ 上下'))

    const view = screen.getByTestId('terminal-split-view')
    expect(view).toHaveClass('is-split-v')
    expect(surface('t1')).not.toHaveClass('terminal-surface--hidden')
    expect(surface('t2')).not.toHaveClass('terminal-surface--hidden')
  })

  it('配对 tab 被关闭时自动清除分屏', async () => {
    mocks.tickets = [makeTicket('t1'), makeTicket('t2')]
    renderPage()
    await screen.findByTestId('terminal-surface-t1')
    fireEvent.click(screen.getByText('终端 2'))
    await screen.findByTestId('terminal-surface-t2')

    fireEvent.click(screen.getByTestId('terminal-split-toggle'))
    fireEvent.click(screen.getByText('⬌ 左右'))
    expect(screen.getByTestId('terminal-split-view')).toHaveClass('is-split-h')

    // 关闭配对 tab（t1）的标签页：分屏自动清除，只剩 t2 可见
    const closes = screen.getAllByRole('button', { name: '关闭标签页' })
    fireEvent.click(closes[0])
    expect(screen.queryByTestId('terminal-split-view')).not.toBeInTheDocument()
    expect(screen.queryByTestId('terminal-surface-t1')).not.toBeInTheDocument()
    expect(surface('t2')).not.toHaveClass('terminal-surface--hidden')
  })
})
