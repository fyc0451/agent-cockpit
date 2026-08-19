import { act, fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { HerdrTerminalModal } from '../features/group-chat/HerdrTerminalModal'

const mocks = vi.hoisted(() => ({
  onData: undefined as ((value: string) => void) | undefined,
  osc52: undefined as ((value: string) => boolean | Promise<boolean>) | undefined,
  writes: [] as number[],
  isTouch: false,
  execCopy: vi.fn(() => true),
}))

vi.mock('../features/terminal/touchScroll', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../features/terminal/touchScroll')>()
  return {
    ...actual,
    isTouchTerminal: () => mocks.isTouch,
  }
})

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class { fit() {} },
}))

vi.mock('@xterm/addon-webgl', () => ({
  WebglAddon: class {
    onContextLoss() {}
    dispose() {}
  },
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 120
    rows = 36
    loadAddon() {}
    open() {}
    readonly options = { disableStdin: true }
    readonly parser = {
      registerOscHandler: (code: number, handler: (value: string) => boolean | Promise<boolean>) => {
        if (code === 52) mocks.osc52 = handler
        return { dispose() {} }
      },
    }
    write(data: unknown, callback?: () => void) {
      mocks.writes.push(typeof data === 'string' ? data.length : (data as Uint8Array).byteLength)
      callback?.()
    }
    refresh() {}
    dispose() {}
    onData(callback: (value: string) => void) {
      mocks.onData = callback
      return { dispose() {} }
    }
  },
}))

const herdrMocks = vi.hoisted(() => ({
  compose: vi.fn(),
  detach: vi.fn(),
  untile: vi.fn(),
  split: vi.fn(),
  snapshot: vi.fn(async () => ({
    panes: [
      {
        pane_id: 'w1:p1',
        session: 'demo-1',
        agent: 'grok',
        agent_status: 'idle',
        cwd: '',
        cwd_name: '',
        display_name: 'BrownDesert',
        mail_name: 'BrownDesert',
        tab_id: 't1',
        focused: true,
      },
      {
        pane_id: 'w1:p2',
        session: 'demo-1',
        agent: 'codex',
        agent_status: 'idle',
        cwd: '',
        cwd_name: '',
        display_name: 'Codex',
        mail_name: 'Codex',
        tab_id: 't1',
        focused: false,
      },
    ],
  })),
}))

const fileMocks = vi.hoisted(() => ({
  list: vi.fn(),
  read: vi.fn(),
  upload: vi.fn(),
  recordLine: vi.fn(async (_session: string, _text: string, _to: string) => {}),
}))

vi.mock('../api/legacyHerdr', () => ({
  openHerdrTerminal: () => Promise.resolve({ id: 'term-1', label: 'term-1' }),
  closeHerdrTerminal: vi.fn(),
  herdrTerminalWebSocketUrl: () => 'ws://localhost/api/term/term-1?replay=1',
  fetchHerdrSnapshot: herdrMocks.snapshot,
  composeSessionLayout: herdrMocks.compose,
  detachPaneLayout: herdrMocks.detach,
  untileSessionLayout: herdrMocks.untile,
  splitPaneLayout: herdrMocks.split,
}))

vi.mock('../api/auth', () => ({
  requireAuthenticated: vi.fn(async () => {}),
}))

vi.mock('../api/chatSession', () => ({
  fetchSessionDirList: fileMocks.list,
  fetchSessionFileContent: fileMocks.read,
  uploadChatFile: fileMocks.upload,
  recordTerminalLine: (session: string, text: string, to: string) =>
    fileMocks.recordLine(session, text, to),
}))

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

class FakeWebSocket {
  static readonly OPEN = 1
  static instance: FakeWebSocket | null = null
  readonly OPEN = 1
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instance = this
  }
  send(data: unknown) {
    this.sent.push(data)
  }
  close() {}
}

describe('HerdrTerminalModal 全屏', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', FakeResizeObserver)
    vi.stubGlobal('WebSocket', FakeWebSocket)
    FakeWebSocket.instance = null
    mocks.onData = undefined
    mocks.osc52 = undefined
    mocks.writes = []
    mocks.isTouch = false
    mocks.execCopy.mockClear()
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: mocks.execCopy,
    })
    window.localStorage.removeItem('term-font-size')
    herdrMocks.compose.mockReset()
    herdrMocks.detach.mockReset()
    herdrMocks.untile.mockReset()
    herdrMocks.split.mockReset()
    herdrMocks.snapshot.mockReset()
    herdrMocks.snapshot.mockResolvedValue({
      panes: [
        {
          pane_id: 'w1:p1',
          session: 'demo-1',
          agent: 'grok',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'BrownDesert',
          mail_name: 'BrownDesert',
          tab_id: 't1',
          focused: true,
        },
        {
          pane_id: 'w1:p2',
          session: 'demo-1',
          agent: 'codex',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'Codex',
          mail_name: 'Codex',
          tab_id: 't1',
          focused: false,
        },
      ],
    })
    fileMocks.list.mockReset()
    fileMocks.read.mockReset()
    fileMocks.upload.mockReset()
    fileMocks.recordLine.mockReset()
    fileMocks.list.mockImplementation(async (_session: string, path: string) => ({
      path,
      type: null,
      entries: path.endsWith('/src')
        ? [{ name: 'main.ts', type: 'file', size: 12, ext: '.ts' }]
        : [
            { name: 'src', type: 'dir', size: 0, ext: '' },
            { name: 'README.md', type: 'file', size: 12, ext: '.md' },
          ],
    }))
    fileMocks.read.mockImplementation(async (_session: string, path: string) => ({
      path,
      text: '# hello',
      binary: false,
      size: 7,
    }))
    fileMocks.upload.mockImplementation(async (_session: string, file: File) => ({
      path: `cockpit-inbox/${file.name}`,
      absolutePath: `/repo/cockpit-inbox/${file.name}`,
      filename: file.name,
      size: file.size,
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('可切换全屏，并用 Escape 退出', () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    const surface = screen.getByTestId('herdr-terminal-surface')
    const modal = surface.closest('.gc-herdr-terminal-modal')
    expect(modal).toHaveClass('is-fullscreen')
    expect(screen.getByRole('button', { name: '退出全屏' })).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(modal).not.toHaveClass('is-fullscreen')
    expect(screen.getByRole('button', { name: '全屏' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '全屏' }))
    expect(modal).toHaveClass('is-fullscreen')
  })

  it('关闭钮钉在顶栏右侧，不被工具按钮挤出', () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    const close = screen.getByRole('button', { name: '关闭终端' })
    const head = close.closest('.gc-herdr-terminal-head')
    const actions = head?.querySelector('.gc-terminal-actions')
    expect(head).not.toBeNull()
    expect(actions?.contains(close)).toBe(false)
    expect(head?.lastElementChild).toBe(close)
  })

  it('replay_complete 后仍挡输入，队列空并安静后才揭开', async () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    expect(await screen.findByRole('status')).toHaveTextContent('正在加载历史输出…')
    const socket = FakeWebSocket.instance
    expect(socket).not.toBeNull()

    act(() => {
      mocks.onData?.('x')
    })
    expect(socket!.sent).toHaveLength(0)

    act(() => {
      socket!.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
    })
    expect(screen.getByRole('status')).toHaveTextContent('正在加载历史输出…')
    act(() => {
      mocks.onData?.('y')
    })
    expect(socket!.sent).toHaveLength(0)

    await vi.waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    act(() => {
      mocks.onData?.('x')
    })
    expect(socket!.sent).toEqual(['x'])
  })

  it('回放首帧只写尾部 8 KiB，之后帧全量写入', async () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    await screen.findByRole('status')
    const socket = FakeWebSocket.instance!

    const replay = new Uint8Array(20 * 1024)
    act(() => {
      socket.onmessage?.({ data: replay.buffer })
    })
    expect(mocks.writes).toEqual([8 * 1024])

    act(() => {
      socket.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
    })
    const live = new Uint8Array(20 * 1024)
    act(() => {
      socket.onmessage?.({ data: live.buffer })
    })
    // 20 KiB 实时帧按 16 KiB 分片写入，后续片经 setTimeout(0) 让步
    await vi.waitFor(() => {
      expect(mocks.writes).toEqual([8 * 1024, 16 * 1024, 4 * 1024])
    })
  })

  it('桌面端不显示触屏输入条', () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    expect(screen.queryByTestId('herdr-terminal-inline-input')).not.toBeInTheDocument()
  })

  it('触屏默认收起输入条，点 ✎ 展开/收起', async () => {
    mocks.isTouch = true
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    expect(screen.queryByTestId('herdr-terminal-inline-input')).not.toBeInTheDocument()
    const toggle = screen.getByTestId('herdr-terminal-input-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)
    expect(await screen.findByTestId('herdr-terminal-inline-input')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(toggle)
    expect(screen.queryByTestId('herdr-terminal-inline-input')).not.toBeInTheDocument()
  })

  it('触屏显示输入条，揭开后发送文字并补回车', async () => {
    mocks.isTouch = true
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    fireEvent.click(screen.getByTestId('herdr-terminal-input-toggle'))
    const field = await screen.findByTestId('herdr-terminal-inline-input')
    expect(field).toBeDisabled()
    fireEvent.change(field, { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(FakeWebSocket.instance?.sent ?? []).not.toContain('hello\r')

    act(() => {
      FakeWebSocket.instance?.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
    })
    await vi.waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    expect(field).not.toBeDisabled()
    fireEvent.change(field, { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(FakeWebSocket.instance?.sent).toContain('hello\r')
    expect(field).toHaveValue('')
    await vi.waitFor(() => {
      expect(fileMocks.recordLine).toHaveBeenCalledWith('demo-1', 'hello', 'BrownDesert')
    })
  })

  it('焦点在空 shell 时终端输入不进瀑布流', async () => {
    herdrMocks.snapshot.mockResolvedValue({
      panes: [
        {
          pane_id: 'w1:p3',
          session: 'demo-1',
          agent: '',
          agent_status: 'unknown',
          cwd: '',
          cwd_name: '',
          display_name: '',
          mail_name: '',
          tab_id: 't3',
          focused: true,
        },
      ],
    })
    mocks.isTouch = true
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    fireEvent.click(screen.getByTestId('herdr-terminal-input-toggle'))
    const field = await screen.findByTestId('herdr-terminal-inline-input')
    act(() => {
      FakeWebSocket.instance?.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
    })
    await vi.waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })
    fireEvent.change(field, { target: { value: 'vim' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(FakeWebSocket.instance?.sent).toContain('vim\r')
    await vi.waitFor(() => {
      expect(herdrMocks.snapshot).toHaveBeenCalled()
    })
    expect(fileMocks.recordLine).not.toHaveBeenCalled()
  })

  it('揭开后屏幕键盘发送 Ctrl-C，布局左右组合两个 Agent', async () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    expect(await screen.findByRole('status')).toHaveTextContent('正在加载历史输出…')
    act(() => {
      FakeWebSocket.instance?.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
    })
    await vi.waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: '展开按键' }))
    fireEvent.click(screen.getByRole('button', { name: 'Ctrl-C' }))
    expect(FakeWebSocket.instance?.sent).toContain('\x03')

    fireEvent.click(screen.getByRole('button', { name: '放大字体' }))
    expect(screen.getByTestId('term-font-size')).toHaveTextContent('14')
    fireEvent.click(screen.getByRole('button', { name: '缩小字体' }))
    expect(screen.getByTestId('term-font-size')).toHaveTextContent('13')

    fireEvent.click(screen.getByRole('button', { name: '终端布局' }))
    fireEvent.click(screen.getByRole('button', { name: '⬌ 左右' }))
    await vi.waitFor(() => {
      expect(herdrMocks.compose).toHaveBeenCalledWith('demo-1', ['w1:p1', 'w1:p2'], 'horizontal')
    })
  })

  it('布局面板可勾选任意 2-4 个 pane 再组合，不限于一对', async () => {
    herdrMocks.snapshot.mockResolvedValue({
      panes: [
        {
          pane_id: 'w1:p1',
          session: 'demo-1',
          agent: 'grok',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'BrownDesert',
          mail_name: 'BrownDesert',
          tab_id: 't1',
          focused: true,
        },
        {
          pane_id: 'w1:p2',
          session: 'demo-1',
          agent: 'codex',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'Codex',
          mail_name: 'Codex',
          tab_id: 't1',
          focused: false,
        },
        {
          pane_id: 'w1:p3',
          session: 'demo-1',
          agent: 'claude',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'Claude',
          mail_name: 'Claude',
          tab_id: 't1',
          focused: false,
        },
        {
          pane_id: 'w1:p4',
          session: 'demo-1',
          agent: 'opencode',
          agent_status: 'idle',
          cwd: '',
          cwd_name: '',
          display_name: 'OpenCode',
          mail_name: 'OpenCode',
          tab_id: 't2',
          focused: false,
        },
      ],
    })
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '终端布局' }))

    expect(await screen.findByTestId('herdr-terminal-layout-list')).toBeInTheDocument()
    expect(screen.getByText(/你所在的分屏组：t1 · 3 个 pane/)).toBeInTheDocument()
    const grok = screen.getByRole('checkbox', { name: /BrownDesert/ })
    const codex = screen.getByRole('checkbox', { name: /Codex/ })
    const claude = screen.getByRole('checkbox', { name: /Claude/ })
    const opencode = screen.getByRole('checkbox', { name: /OpenCode/ })
    expect(grok).toBeChecked()
    expect(codex).toBeChecked()
    expect(claude).toBeChecked()
    expect(opencode).not.toBeChecked()

    fireEvent.click(claude)
    fireEvent.click(opencode)
    fireEvent.change(screen.getByLabelText('组合方向'), { target: { value: 'vertical' } })
    fireEvent.click(screen.getByRole('button', { name: '⧉ 组合选中 pane' }))
    await vi.waitFor(() => {
      expect(herdrMocks.compose).toHaveBeenCalledWith(
        'demo-1',
        ['w1:p1', 'w1:p2', 'w1:p4'],
        'vertical',
      )
    })

    fireEvent.click(codex)
    fireEvent.click(opencode)
    fireEvent.click(screen.getByRole('button', { name: '⧉ 组合选中 pane' }))
    expect(await screen.findByText('请勾选 2-4 个 pane')).toBeInTheDocument()
  })

  it('桌面端从当前 pane 工作目录打开文件侧栏并浏览文件', async () => {
    herdrMocks.snapshot.mockResolvedValue({
      panes: [
        {
          pane_id: 'w1:p1',
          session: 'demo-1',
          agent: 'grok',
          agent_status: 'idle',
          cwd: '/repo/current',
          cwd_name: 'current',
          display_name: 'BrownDesert',
          mail_name: 'BrownDesert',
          tab_id: 't1',
          focused: true,
        },
      ],
    })
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '打开文件' }))
    expect(await screen.findByRole('button', { name: '进入 src' })).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: '终端文件' })).toBeInTheDocument()
    expect(fileMocks.list).toHaveBeenCalledWith('demo-1', '/repo/current')

    fireEvent.click(screen.getByRole('button', { name: '进入 src' }))
    expect(await screen.findByRole('button', { name: '打开 main.ts' })).toBeInTheDocument()
    expect(fileMocks.list).toHaveBeenCalledWith('demo-1', '/repo/current/src')

    fireEvent.click(screen.getByRole('button', { name: '打开 main.ts' }))
    expect(await screen.findByText('# hello')).toBeInTheDocument()
    expect(fileMocks.read).toHaveBeenCalledWith('demo-1', '/repo/current/src/main.ts')
    expect(screen.getByRole('button', { name: '返回文件列表' })).toBeInTheDocument()
  })

  it('暂存 Herdr OSC 52 文本并通过按钮复制到浏览器剪贴板', async () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '复制到剪贴板' }))
    expect(screen.getByText('还没有可复制的文字，请先在 Herdr TUI 里复制')).toBeInTheDocument()

    act(() => {
      mocks.osc52?.(`c;${btoa('copied text')}`)
    })
    expect(screen.getByText('文字已暂存，点击复制按钮即可使用')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '复制到剪贴板' }))
    expect(await screen.findByText('已复制到剪贴板')).toBeInTheDocument()
    expect(mocks.execCopy).toHaveBeenCalledWith('copy')
  })

  it('上传多个文件并把绝对 @路径插入当前终端但不提交', async () => {
    render(<HerdrTerminalModal session="demo-1" onClose={vi.fn()} />)
    expect(await screen.findByRole('status')).toHaveTextContent('正在加载历史输出…')
    await vi.waitFor(() => {
      expect(FakeWebSocket.instance).not.toBeNull()
    })
    await act(async () => {
      FakeWebSocket.instance?.onmessage?.({ data: JSON.stringify({ type: 'replay_complete' }) })
      await new Promise((resolve) => window.setTimeout(resolve, 100))
    })
    expect(screen.queryByText('正在加载历史输出…')).not.toBeInTheDocument()

    const first = new File(['png'], 'shot.png', { type: 'image/png' })
    const second = new File(['txt'], 'notes.txt', { type: 'text/plain' })
    await act(async () => {
      fireEvent.change(screen.getByTestId('herdr-terminal-upload'), {
        target: { files: [first, second] },
      })
      await new Promise((resolve) => window.setTimeout(resolve, 0))
    })

    await vi.waitFor(() => {
      expect(fileMocks.upload).toHaveBeenCalledTimes(2)
    })
    const inserted = '@/repo/cockpit-inbox/shot.png @/repo/cockpit-inbox/notes.txt '
    await vi.waitFor(() => {
      expect(FakeWebSocket.instance?.sent).toContain(inserted)
    })
    expect(inserted).not.toContain('\r')
    expect(screen.getByText('已上传并插入 2 个文件')).toBeInTheDocument()
  })
})
