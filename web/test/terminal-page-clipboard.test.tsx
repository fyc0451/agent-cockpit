import { act, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import type { TerminalStreamHandlers, TerminalTicketView } from '../api/terminals'
import type { Project, Workspace } from '../api/types'
import { TerminalPage } from '../pages/TerminalPage'

const mocks = vi.hoisted(() => ({
  osc52: [] as Array<(value: string) => boolean | Promise<boolean>>,
  copied: [] as string[],
  handlers: [] as TerminalStreamHandlers[],
}))

vi.mock('../features/terminal/touchScroll', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../features/terminal/touchScroll')>()
  return {
    ...actual,
    isTouchTerminal: () => false,
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
    readonly parser = {
      registerOscHandler: (_code: number, handler: (value: string) => boolean | Promise<boolean>) => {
        mocks.osc52.push(handler)
        return { dispose() {} }
      },
    }
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

function ticket(id: string): TerminalTicketView {
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

const tickets = [ticket('t1'), ticket('t2')]

vi.mock('../api/terminals', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/terminals')>()
  return {
    ...actual,
    listTerminalTickets: () => Promise.resolve({ items: tickets, next_cursor: null }),
    connectTerminalStream: (
      _ids: unknown,
      _fence: unknown,
      handlers: TerminalStreamHandlers,
    ) => {
      mocks.handlers.push(handlers)
      return {
        sendInput: vi.fn(),
        sendResize: vi.fn(),
        close: vi.fn(),
        ready: true,
      }
    },
  }
})

vi.mock('../features/ProjectScope', () => ({
  ProjectScope: ({ children }: { slug: string; children: (project: Project) => ReactNode }) => (
    <>{children({ slug: 'demo', name: 'Demo', project_id: 'p1' })}</>
  ),
}))

vi.mock('../features/WorkspaceScope', () => ({
  WorkspaceScope: ({ children }: { project: Project; children: (workspace: Workspace) => ReactNode }) => (
    <>{children({ id: 'w1', workspace_id: 'w1', name: 'ws', version: 1 })}</>
  ),
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

describe('TerminalPage OSC 52 剪贴板', () => {
  beforeEach(() => {
    mocks.osc52 = []
    mocks.copied = []
    mocks.handlers = []
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn(() => {
        const area = document.querySelector('textarea')
        if (area) mocks.copied.push(area.value)
        return true
      }),
    })
  })

  it('按 ticket 隔离暂存值，按钮只复制当前 tab', async () => {
    renderPage()
    await screen.findByTestId('terminal-tabs')
    await vi.waitFor(() => expect(mocks.osc52).toHaveLength(1))

    act(() => {
      mocks.osc52[0](`c;${btoa('first terminal')}`)
    })
    fireEvent.click(screen.getByTitle('终端 2（未连接，点击重新连接）'))
    await vi.waitFor(() => expect(mocks.osc52).toHaveLength(2))
    act(() => {
      mocks.osc52[1](`c;${btoa('second terminal')}`)
    })

    fireEvent.click(screen.getByTitle('终端 1'))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '复制到剪贴板' }))
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(mocks.copied).toEqual(['first terminal']))

    fireEvent.click(screen.getByTitle('终端 2'))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '复制到剪贴板' }))
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(mocks.copied).toEqual(['first terminal', 'second terminal']))
  })
})
