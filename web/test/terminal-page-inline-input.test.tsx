import { act, fireEvent, render, screen } from '@testing-library/react'
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
}))

vi.mock('../features/terminal/touchScroll', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../features/terminal/touchScroll')>()
  return {
    ...actual,
    isTouchTerminal: () => mocks.isTouch,
    // 触摸滚动桥依赖真实 xterm 实例，本用例只关心输入条，替换为空操作
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

// 页头两个 scope 组件内部走 registry/workspace 查询；输入条用例无关，直接喂固定身份
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

describe('TerminalPage 触屏输入条', () => {
  beforeEach(() => {
    mocks.isTouch = false
    mocks.sendInput = vi.fn()
    mocks.handlers = undefined
  })

  it('非触屏不渲染输入条', async () => {
    renderPage()
    await screen.findByTestId('terminal-tabs')
    expect(screen.queryByTestId('terminal-inline-input')).not.toBeInTheDocument()
  })

  it('触屏默认收起输入条，点 ✎ 展开/收起', async () => {
    mocks.isTouch = true
    renderPage()
    await screen.findByTestId('terminal-tabs')

    expect(screen.queryByTestId('terminal-inline-input')).not.toBeInTheDocument()
    const toggle = screen.getByTestId('terminal-input-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)
    expect(await screen.findByTestId('terminal-inline-input')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(toggle)
    expect(screen.queryByTestId('terminal-inline-input')).not.toBeInTheDocument()
  })

  it('触屏渲染输入条，live 前禁用，发送补回车并清空', async () => {
    mocks.isTouch = true
    renderPage()

    fireEvent.click(await screen.findByTestId('terminal-input-toggle'))
    const field = await screen.findByTestId('terminal-inline-input')
    expect(field).toBeDisabled()

    await driveToLive()
    await vi.waitFor(() => expect(field).not.toBeDisabled())

    fireEvent.change(field, { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(mocks.sendInput).toHaveBeenCalledWith('hello\r')
    expect(field).toHaveValue('')
  })

  it('空内容不发送', async () => {
    mocks.isTouch = true
    renderPage()

    fireEvent.click(await screen.findByTestId('terminal-input-toggle'))
    const field = await screen.findByTestId('terminal-inline-input')
    await driveToLive()
    await vi.waitFor(() => expect(field).not.toBeDisabled())

    fireEvent.change(field, { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    fireEvent.submit(field.closest('form')!)
    expect(mocks.sendInput).not.toHaveBeenCalled()
  })
})
