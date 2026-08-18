import { act, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import type { TerminalStreamHandlers, TerminalTicketView } from '../api/terminals'
import type { Project, Workspace } from '../api/types'
import { TerminalPage } from '../pages/TerminalPage'

const mocks = vi.hoisted(() => ({
  isTouch: false,
  sendInput: undefined as unknown as ReturnType<typeof vi.fn>,
  handlers: undefined as TerminalStreamHandlers | undefined,
  onData: undefined as ((value: string) => void) | undefined,
}))

vi.mock('../features/terminal/touchScroll', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../features/terminal/touchScroll')>()
  return {
    ...actual,
    isTouchTerminal: () => mocks.isTouch,
    // 触摸滚动桥依赖真实 xterm 实例，本用例只关心键盘栏，替换为空操作
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
    onData(callback: (value: string) => void) {
      mocks.onData = callback
      return { dispose() {} }
    }
    parser = {
      registerOscHandler() {
        return { dispose() {} }
      },
    }
  },
}))

const ticketView: TerminalTicketView = {
  ticket: {
    ticket_id: 't1',
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

vi.mock('../api/terminals', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/terminals')>()
  return {
    ...actual,
    listTerminalTickets: () => Promise.resolve({ items: [ticketView], next_cursor: null }),
    connectTerminalStream: (
      _ids: unknown,
      _fence: unknown,
      handlers: TerminalStreamHandlers,
    ) => {
      mocks.handlers = handlers
      return {
        sendInput: mocks.sendInput,
        sendResize: vi.fn(),
        close: vi.fn(),
        ready: true,
      }
    },
  }
})

// 页头两个 scope 组件内部走 registry/workspace 查询；键盘栏用例无关，直接喂固定身份
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

/** 等流挂上后推进 replay_start → replay_complete，等 load gate 揭开进入 live */
async function driveToLive() {
  await vi.waitFor(() => expect(mocks.handlers).toBeDefined())
  act(() => {
    mocks.handlers!.onReplayStart()
    mocks.handlers!.onReplayComplete(false)
  })
}

/** 触屏下展开键栏并等终端进入 live（键钮解禁即 phase==='live'） */
async function openKeysLive() {
  fireEvent.click(await screen.findByRole('button', { name: '展开按键' }))
  const bar = screen.getByTestId('terminal-keys')
  await driveToLive()
  await vi.waitFor(() =>
    expect(within(bar).getByTitle('方向键上')).not.toBeDisabled(),
  )
  return bar
}

describe('TerminalPage 键盘栏', () => {
  beforeEach(() => {
    mocks.isTouch = false
    mocks.sendInput = vi.fn()
    mocks.handlers = undefined
    mocks.onData = undefined
  })

  it('桌面也渲染 ⌨ 按钮，点开即展开键栏，live 前键钮禁用', async () => {
    renderPage()

    const toggle = await screen.findByRole('button', { name: '展开按键' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('terminal-keys')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    const bar = screen.getByTestId('terminal-keys')
    expect(screen.getByRole('button', { name: '收起按键' })).toHaveAttribute('aria-expanded', 'true')
    // 未 live：键钮全部禁用
    expect(within(bar).getByTitle('方向键上')).toBeDisabled()

    await driveToLive()
    await vi.waitFor(() => expect(within(bar).getByTitle('方向键上')).not.toBeDisabled())
  })

  it('点方向键上发送 \\x1b[A', async () => {
    renderPage()
    const bar = await openKeysLive()

    fireEvent.click(within(bar).getByTitle('方向键上'))
    expect(mocks.sendInput).toHaveBeenCalledWith('\x1b[A')
  })

  it('Ctrl 修饰切换后再点方向键修饰生效，且发送后修饰复位', async () => {
    renderPage()
    const bar = await openKeysLive()

    const ctrl = within(bar).getByText('Ctrl')
    fireEvent.click(ctrl)
    expect(ctrl).toHaveClass('is-active')
    expect(mocks.sendInput).not.toHaveBeenCalled()

    fireEvent.click(within(bar).getByTitle('方向键上'))
    // encodeTermKey：Ctrl+ArrowUp → CSI 1;5A
    expect(mocks.sendInput).toHaveBeenCalledWith('\x1b[1;5A')
    // 采用 encodeTermKey 返回的 EMPTY_MODIFIERS：修饰键复位
    expect(ctrl).not.toHaveClass('is-active')
  })

  it('DECSET 1004 焦点报告不转发 PTY，普通输入照常', async () => {
    renderPage()
    await openKeysLive()
    await vi.waitFor(() => expect(mocks.onData).toBeDefined())

    act(() => mocks.onData!('\x1b[I'))
    act(() => mocks.onData!('\x1b[O'))
    expect(mocks.sendInput).not.toHaveBeenCalled()

    act(() => mocks.onData!('a'))
    expect(mocks.sendInput).toHaveBeenCalledWith('a')
  })
})
