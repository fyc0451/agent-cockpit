import { useEffect, useRef, useState } from 'react'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { recordTerminalLine, uploadChatFile } from '../../api/chatSession'
import { focusedMemberRecipient } from './model'
import {
  closeHerdrTerminal,
  composeSessionLayout,
  detachPaneLayout,
  fetchHerdrSnapshot,
  herdrTerminalWebSocketUrl,
  openHerdrTerminal,
  splitPaneLayout,
  untileSessionLayout,
} from '../../api/legacyHerdr'
import {
  attachWebgl,
  createLoadGate,
  createTermWriter,
  REPLAY_TAIL,
} from '../terminal/termRender'
import { enableTermKeyboardGuard, enableTermTouchScroll, isTouchTerminal } from '../terminal/touchScroll'
import { installH5TouchRecorder } from '../terminal/h5Debug'
import {
  EMPTY_MODIFIERS,
  encodeTermKey,
  isTermFocusReport,
  TERM_KEYS,
  type TermModifiers,
} from '../terminal/termKeys'
import {
  COMPOSE_MAX_PANES,
  COMPOSE_MIN_PANES,
  defaultComposePicks,
  layoutGroup,
  paneComposeLabel,
  panesForSession,
  pickLayoutTarget,
  pickPairIds,
  sortComposePanes,
  type LayoutPane,
} from '../terminal/termLayout'
import { decodeOsc52, writeBrowserClipboard } from '../terminal/termClipboard'
import { feedTermLedger } from '../terminal/termLedger'
import { loadTermFontSize, saveTermFontSize } from '../terminal/termFont'
import { TermFontControls } from '../terminal/TermFontControls'
import { HerdrTerminalFilePanel } from './HerdrTerminalFilePanel'

const textEncoder = new TextEncoder()

interface HerdrTerminalModalProps {
  session: string
  onClose: () => void
}

export function HerdrTerminalModal({ session, onClose }: HerdrTerminalModalProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const readyRef = useRef(false)
  const clipboardRef = useRef('')
  const lineBufRef = useRef('')
  const uploadRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [fullscreen, setFullscreen] = useState(true)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState('')
  const [inputOpen, setInputOpen] = useState(false)
  const [keysOpen, setKeysOpen] = useState(false)
  const [mods, setMods] = useState<TermModifiers>(EMPTY_MODIFIERS)
  const [layoutOpen, setLayoutOpen] = useState(false)
  const [layoutBusy, setLayoutBusy] = useState(false)
  const [layoutNote, setLayoutNote] = useState<string | null>(null)
  const [layoutPanes, setLayoutPanes] = useState<LayoutPane[]>([])
  const [layoutTarget, setLayoutTarget] = useState<LayoutPane | null>(null)
  const [layoutHint, setLayoutHint] = useState('打开后识别当前分屏组…')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [composeOrientation, setComposeOrientation] = useState<'horizontal' | 'vertical'>('horizontal')
  const [filesOpen, setFilesOpen] = useState(false)
  const [fileRoot, setFileRoot] = useState<string | null>(null)
  const [fileNote, setFileNote] = useState<string | null>(null)
  const [clipboardNote, setClipboardNote] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadNote, setUploadNote] = useState<string | null>(null)
  const [fontSize, setFontSize] = useState(loadTermFontSize)
  const applyFontRef = useRef<(size: number) => void>(() => {})
  const ledgerToRef = useRef<string | null>(null)
  const touch = isTouchTerminal()

  // H5 真机触控事件记录仪（?debug=1 门控，无参数时零副作用）。
  useEffect(() => installH5TouchRecorder(), [])

  useEffect(() => {
    let disposed = false
    const refresh = async () => {
      try {
        const snap = await fetchHerdrSnapshot()
        if (disposed) return
        ledgerToRef.current = focusedMemberRecipient(snap, session)
      } catch {
        if (!disposed) ledgerToRef.current = null
      }
    }
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 2000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [session])

  useEffect(() => {
    if (!fullscreen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setFullscreen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [fullscreen])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let disposed = false
    let termId: string | null = null
    let socket: WebSocket | null = null
    let ready = false
    // 首帧二进制即 ?replay=1 的历史回放，只写尾部，之后正常全量写。
    let awaitingReplay = true
    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: loadTermFontSize(),
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      disableStdin: true,
      theme: { background: '#111418', foreground: '#e8eaed', cursor: '#e8eaed' },
    })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(host)
    fit.fit()
    clipboardRef.current = ''
    setClipboardNote(null)
    const osc52 = terminal.parser.registerOscHandler(52, (data) => {
      const text = decodeOsc52(data)
      if (!text) return true
      clipboardRef.current = text
      if (!disposed) setClipboardNote('文字已暂存，点击复制按钮即可使用')
      if (window.isSecureContext && typeof navigator.clipboard?.writeText === 'function') {
        void navigator.clipboard.writeText(text).then(
          () => {
            if (!disposed) setClipboardNote('已复制到剪贴板')
          },
          () => {
            if (!disposed) setClipboardNote('文字已暂存，点击复制按钮即可使用')
          },
        )
      }
      return true
    })
    const detachWebgl = attachWebgl(terminal)
    // H5 触屏：单指拖动转 wheel（mouseTracking/alternate buffer），tap 合成点击，
    // 并拦截隐藏 textarea 获焦弹软键盘（仅触屏设备生效，1.0 同款）。
    const detachTouchScroll = enableTermTouchScroll(host, terminal)
    const detachKeyboardGuard = enableTermKeyboardGuard(host)
    let gate: ReturnType<typeof createLoadGate>
    const writer = createTermWriter((data, done) => {
      terminal.write(data, done)
    }, () => gate.noteIdle())
    gate = createLoadGate({
      isBusy: () => writer.busy(),
      isAlternate: () => {
        const type = (terminal as { buffer?: { active?: { type?: string } } }).buffer?.active?.type
        return !type || type === 'alternate'
      },
      onReady: () => {
        if (disposed) return
        ready = true
        readyRef.current = true
        terminal.options.disableStdin = false
        setLoading(false)
      },
    })

    const dimensions = () => ({
      cols: Math.min(500, Math.max(40, terminal.cols)),
      rows: Math.min(300, Math.max(12, terminal.rows)),
    })
    const sendResize = () => {
      if (!ready) return
      fit.fit()
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', ...dimensions() }))
      }
    }
    applyFontRef.current = (size: number) => {
      terminal.options.fontSize = size
      fit.fit()
      if (ready && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', ...dimensions() }))
      }
    }
    const resizeObserver = new ResizeObserver(sendResize)
    resizeObserver.observe(host)
    const input = terminal.onData((value) => {
      // 揭开前丢掉键鼠，不要攒着等主线程空了再灌进 TUI。
      if (!ready) return
      // 1.0：DECSET 1004 焦点报告不要转发给 PTY，否则切 tab 会整屏重绘。
      if (isTermFocusReport(value)) return
      if (socket?.readyState === WebSocket.OPEN) socket.send(value)
      const next = feedTermLedger(lineBufRef.current, value)
      lineBufRef.current = next.buffer
      const dest = ledgerToRef.current
      if (next.line && dest) void recordTerminalLine(session, next.line, dest)
    })

    const connect = async () => {
      try {
        const created = await openHerdrTerminal(session, dimensions().cols, dimensions().rows)
        if (disposed) {
          await closeHerdrTerminal(created.id)
          return
        }
        termId = created.id
        socket = new WebSocket(herdrTerminalWebSocketUrl(created.id))
        socketRef.current = socket
        socket.binaryType = 'arraybuffer'
        socket.onopen = sendResize
        socket.onmessage = (event) => {
          if (typeof event.data === 'string') {
            try {
              const control: unknown = JSON.parse(event.data)
              if (
                typeof control === 'object' &&
                control !== null &&
                'type' in control &&
                control.type === 'replay_complete'
              ) {
                awaitingReplay = false
                gate.noteReplayComplete()
                return
              }
            } catch {
              // 普通终端输出不是 JSON，直接写入。
            }
            gate.noteOutput()
            writer.queue(textEncoder.encode(event.data))
          } else if (event.data instanceof ArrayBuffer) {
            const data = new Uint8Array(event.data)
            gate.noteOutput()
            if (awaitingReplay) {
              awaitingReplay = false
              writer.queue(data.subarray(Math.max(0, data.byteLength - REPLAY_TAIL)))
            } else {
              writer.queue(data)
            }
          }
        }
        socket.onerror = () => {
          setLoading(false)
          setError('终端连接中断')
        }
        socket.onclose = (event) => {
          setLoading(false)
          if (!disposed && event.code !== 1000) setError('终端已断开')
        }
      } catch (cause) {
        setLoading(false)
        setError(cause instanceof ApiError ? cause.message : String(cause))
      }
    }
    void connect()

    return () => {
      disposed = true
      readyRef.current = false
      socketRef.current = null
      gate.dispose()
      writer.dispose()
      detachTouchScroll()
      detachKeyboardGuard()
      detachWebgl()
      osc52.dispose()
      input.dispose()
      applyFontRef.current = () => {}
      resizeObserver.disconnect()
      socket?.close(1000, 'terminal modal closed')
      terminal.dispose()
      if (termId) void closeHerdrTerminal(termId)
    }
  }, [session])

  const sendBytes = (data: string) => {
    if (!readyRef.current) return false
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    socket.send(data)
    const next = feedTermLedger(lineBufRef.current, data)
    lineBufRef.current = next.buffer
    const dest = ledgerToRef.current
    if (next.line && dest) void recordTerminalLine(session, next.line, dest)
    return true
  }

  const sendInline = () => {
    const text = draft.trim()
    if (!text) return
    // 1.0 termSendInlineInput：发送=提交，补回车，否则文字停在 agent 输入框。
    if (sendBytes(`${text}\r`)) setDraft('')
  }

  const sendKey = (name: string) => {
    const encoded = encodeTermKey(name, mods)
    if (!encoded) return
    if (sendBytes(encoded.seq)) setMods(encoded.mods)
  }

  const applyLayoutSnapshot = (panes: LayoutPane[]) => {
    const target = pickLayoutTarget(panes)
    const { tabId, group } = layoutGroup(target, panes)
    setLayoutPanes(panes)
    setLayoutTarget(target)
    setSelectedIds(defaultComposePicks(panes, target, group))
    if (!panes.length) {
      setLayoutHint('当前 session 没有 pane')
    } else if (group.length <= 1) {
      const label = paneComposeLabel(group[0] || target || panes[0])
      setLayoutHint(
        tabId
          ? `你所在的 tab ${tabId} 已是单 pane（${label}），无需拆开。`
          : '当前 session 没有分屏组',
      )
    } else {
      setLayoutHint(
        `你所在的分屏组：${tabId} · ${group.length} 个 pane · ${group.map(paneComposeLabel).join(' · ')}`,
      )
    }
    return { panes, target, tabId, group }
  }

  const loadLayoutContext = async () => {
    const snap = await fetchHerdrSnapshot()
    return applyLayoutSnapshot(panesForSession(snap.panes, session))
  }

  const toggleLayout = async () => {
    if (layoutOpen) {
      setLayoutOpen(false)
      setLayoutNote(null)
      return
    }
    setLayoutOpen(true)
    setLayoutNote(null)
    setLayoutHint('打开后识别当前分屏组…')
    try {
      await loadLayoutContext()
    } catch (cause) {
      setLayoutHint(cause instanceof ApiError ? cause.message : String(cause))
    }
  }

  const togglePick = (paneId: string) => {
    setSelectedIds((current) =>
      current.includes(paneId) ? current.filter((id) => id !== paneId) : [...current, paneId],
    )
  }

  const runLayout = async (action: 'pair-h' | 'pair-v' | 'detach' | 'untile') => {
    setLayoutBusy(true)
    setLayoutNote(null)
    try {
      const { panes, target, tabId, group } = await loadLayoutContext()
      if (!target) {
        setLayoutNote('找不到当前 pane')
        return
      }
      if (action === 'pair-h' || action === 'pair-v') {
        const pair = pickPairIds(panes, target)
        if (pair.length < 2) {
          setLayoutNote('当前 session 至少需要两个 Agent pane')
          return
        }
        await composeSessionLayout(session, pair, action === 'pair-h' ? 'horizontal' : 'vertical')
        setLayoutNote('已组合为分屏')
        return
      }
      if (action === 'detach') {
        await detachPaneLayout(session, target.pane_id)
        setLayoutNote('已拆出到独立 tab')
        return
      }
      if (!tabId) {
        setLayoutNote('无法确定当前 pane 所在 tab')
        return
      }
      if (group.length <= 1) {
        setLayoutNote('该 tab 只有一个 pane（session 内也没有其他多分屏组）')
        return
      }
      await untileSessionLayout(session, tabId)
      setLayoutNote('已拆开整组')
    } catch (cause) {
      setLayoutNote(cause instanceof ApiError ? cause.message : String(cause))
    } finally {
      setLayoutBusy(false)
    }
  }

  const composeSelected = async () => {
    setLayoutBusy(true)
    setLayoutNote(null)
    try {
      if (selectedIds.length < COMPOSE_MIN_PANES || selectedIds.length > COMPOSE_MAX_PANES) {
        setLayoutNote(`请勾选 ${COMPOSE_MIN_PANES}-${COMPOSE_MAX_PANES} 个 pane`)
        return
      }
      await composeSessionLayout(session, selectedIds, composeOrientation)
      setLayoutNote('已组合为分屏')
    } catch (cause) {
      setLayoutNote(cause instanceof ApiError ? cause.message : String(cause))
    } finally {
      setLayoutBusy(false)
    }
  }

  const splitEmpty = async (mode: 'horizontal' | 'vertical' | 'grid4') => {
    if (!window.confirm('将新建空 shell pane，确认继续吗？')) return
    setLayoutBusy(true)
    setLayoutNote(null)
    try {
      const { target } = await loadLayoutContext()
      if (!target) {
        setLayoutNote('找不到当前 pane')
        return
      }
      await splitPaneLayout(session, target.pane_id, mode)
      setLayoutNote('已分屏(新槽位为空 shell)')
    } catch (cause) {
      setLayoutNote(cause instanceof ApiError ? cause.message : String(cause))
    } finally {
      setLayoutBusy(false)
    }
  }

  const toggleFiles = async () => {
    if (filesOpen) {
      setFilesOpen(false)
      return
    }
    setFileNote(null)
    try {
      const snap = await fetchHerdrSnapshot()
      const panes = snap.panes.filter((pane) => pane.session === session && pane.cwd)
      const target = panes.find((pane) => pane.focused) ?? panes[0]
      if (!target?.cwd) {
        setFileNote('当前终端没有目录信息')
        return
      }
      setFileRoot(target.cwd)
      setFilesOpen(true)
    } catch (cause) {
      setFileNote(cause instanceof ApiError ? cause.message : String(cause))
    }
  }

  const copyServiceClipboard = async () => {
    const text = clipboardRef.current
    if (!text) {
      setClipboardNote('还没有可复制的文字，请先在 Herdr TUI 里复制')
      return
    }
    const copied = await writeBrowserClipboard(text)
    setClipboardNote(copied ? '已复制到剪贴板' : '浏览器无法写入剪贴板')
  }

  const uploadTerminalFiles = async (files: File[]) => {
    if (!files.length || uploading) return
    try {
      await requireAuthenticated()
    } catch (cause) {
      setUploadNote(cause instanceof ApiError ? cause.message : String(cause))
      return
    }
    setUploading(true)
    setUploadNote(`正在上传 ${files.length} 个文件…`)
    const refs: string[] = []
    let failed = 0
    let failure = ''
    for (const file of files) {
      try {
        const saved = await uploadChatFile(session, file)
        refs.push(`@${saved.absolutePath} `)
      } catch (cause) {
        failed += 1
        failure = cause instanceof ApiError ? cause.message : String(cause)
      }
    }
    if (refs.length && sendBytes(refs.join(''))) {
      setUploadNote(
        failed
          ? `已上传并插入 ${refs.length} 个文件，${failed} 个失败：${failure}`
          : `已上传并插入 ${refs.length} 个文件`,
      )
    } else if (refs.length) {
      setUploadNote(`已上传 ${refs.length} 个文件，但终端未连接，未插入路径`)
    } else {
      setUploadNote(`上传失败：${failure || '没有文件上传成功'}`)
    }
    setUploading(false)
  }

  return (
    <div className="gc-modal-bg" onClick={onClose}>
      <div
        className={`gc-herdr-terminal-modal${fullscreen ? ' is-fullscreen' : ''}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="gc-herdr-terminal-head">
          <div className="gc-herdr-terminal-head-copy">
            <h3 className="gc-modal-title">Herdr 终端</h3>
            <p className="gc-modal-sub">{session}</p>
          </div>
          <div className="gc-terminal-actions">
            <TermFontControls
              value={fontSize}
              onChange={(size) => {
                const next = saveTermFontSize(size)
                setFontSize(next)
                applyFontRef.current(next)
              }}
            />
            <button
              type="button"
              className={`gc-terminal-head-button${keysOpen ? ' is-active' : ''}`}
              onClick={() => setKeysOpen((value) => !value)}
              aria-label={keysOpen ? '收起按键' : '展开按键'}
              aria-expanded={keysOpen}
              title={keysOpen ? '收起触屏按键' : '展开触屏按键'}
            >
              <span aria-hidden>⌨</span>
            </button>
            {touch && (
              <button
                type="button"
                className={`gc-terminal-head-button${inputOpen ? ' is-active' : ''}`}
                onClick={() => setInputOpen((value) => !value)}
                aria-label={inputOpen ? '收起输入条' : '展开输入条'}
                aria-expanded={inputOpen}
                title={inputOpen ? '收起输入条' : '展开输入条（点才弹键盘）'}
                data-testid="herdr-terminal-input-toggle"
              >
                <span aria-hidden>✎</span>
              </button>
            )}
            <button
              type="button"
              className={`gc-terminal-head-button${layoutOpen ? ' is-active' : ''}`}
              onClick={() => void toggleLayout()}
              aria-label={layoutOpen ? '收起布局' : '终端布局'}
              aria-expanded={layoutOpen}
              title="一键布局：分屏/拆开/勾选组合"
            >
              <span aria-hidden>🧱</span>
            </button>
            <button
              type="button"
              className={`gc-terminal-head-button gc-terminal-files-toggle${filesOpen ? ' is-active' : ''}`}
              onClick={() => void toggleFiles()}
              aria-label={filesOpen ? '关闭文件' : '打开文件'}
              aria-expanded={filesOpen}
              title={filesOpen ? '关闭文件侧栏' : '浏览当前工作目录'}
            >
              <span aria-hidden>📁</span>
            </button>
            <button
              type="button"
              className="gc-terminal-head-button"
              onClick={() => void copyServiceClipboard()}
              aria-label="复制到剪贴板"
              title="把刚才在 Herdr TUI 里复制的文字放进系统剪贴板"
            >
              <span aria-hidden>📋</span>
            </button>
            <button
              type="button"
              className="gc-terminal-head-button"
              onClick={() => uploadRef.current?.click()}
              disabled={uploading || loading || Boolean(error)}
              aria-label="上传文件"
              title="上传截图或文件，并把 @路径插入当前终端"
            >
              <span aria-hidden>📎</span>
            </button>
            <input
              ref={uploadRef}
              type="file"
              multiple
              hidden
              disabled={uploading || loading || Boolean(error)}
              data-testid="herdr-terminal-upload"
              onChange={(event) => {
                const files = Array.from(event.target.files || [])
                event.target.value = ''
                void uploadTerminalFiles(files)
              }}
            />
            <button
              type="button"
              className="gc-terminal-head-button"
              onClick={() => setFullscreen((value) => !value)}
              aria-label={fullscreen ? '退出全屏' : '全屏'}
              title={fullscreen ? '退出全屏（Esc）' : '全屏'}
            >
              <span aria-hidden>⛶</span>
            </button>
          </div>
          <button
            type="button"
            className="gc-terminal-head-button gc-terminal-close"
            onClick={onClose}
            aria-label="关闭终端"
            title="关闭终端"
          >
            <span aria-hidden>×</span>
          </button>
        </div>
        {keysOpen && (
          <div className="gc-term-keys" role="toolbar" aria-label="手机电脑键盘">
            {TERM_KEYS.map((key) => (
              <button
                key={key.name}
                type="button"
                className={`${key.extra ? 'gc-term-key-extra' : ''}${
                  key.modifier && mods[key.modifier] ? ' is-active' : ''
                }`.trim()}
                title={key.title || key.label}
                disabled={loading || Boolean(error)}
                onClick={() => {
                  if (key.modifier) {
                    setMods((current) => ({ ...current, [key.modifier!]: !current[key.modifier!] }))
                    return
                  }
                  sendKey(key.name)
                }}
              >
                {key.label}
              </button>
            ))}
          </div>
        )}
        {layoutOpen && (
          <div className="gc-term-layout" data-testid="herdr-terminal-layout">
            <div className="gc-term-layout-hint">{layoutHint}</div>
            <div className="gc-term-layout-quick">
              <button type="button" disabled={layoutBusy} onClick={() => void runLayout('pair-h')}>
                ⬌ 左右
              </button>
              <button type="button" disabled={layoutBusy} onClick={() => void runLayout('pair-v')}>
                ⬍ 上下
              </button>
              <button type="button" disabled={layoutBusy} onClick={() => void runLayout('detach')}>
                拆出当前
              </button>
              <button type="button" disabled={layoutBusy} onClick={() => void runLayout('untile')}>
                拆开整组
              </button>
            </div>
            <details className="gc-term-layout-advanced">
              <summary>高级：插入空 shell 槽</summary>
              <p className="gc-term-layout-hint">这些操作会新建空 shell pane，仅在确实需要额外终端时使用。</p>
              <div className="gc-term-layout-quick">
                <button type="button" disabled={layoutBusy} onClick={() => void splitEmpty('horizontal')}>
                  ⬌ 空槽左右
                </button>
                <button type="button" disabled={layoutBusy} onClick={() => void splitEmpty('vertical')}>
                  ⬍ 空槽上下
                </button>
                <button type="button" disabled={layoutBusy} onClick={() => void splitEmpty('grid4')}>
                  ⊞ 空槽四分
                </button>
              </div>
            </details>
            <div className="gc-term-layout-compose">
              <span className="gc-term-layout-label">组合分屏</span>
              <select
                aria-label="组合方向"
                value={composeOrientation}
                onChange={(event) =>
                  setComposeOrientation(event.target.value === 'vertical' ? 'vertical' : 'horizontal')
                }
              >
                <option value="horizontal">单行水平</option>
                <option value="vertical">单列垂直</option>
              </select>
              <button type="button" disabled={layoutBusy} onClick={() => void composeSelected()}>
                ⧉ 组合选中 pane
              </button>
              <span className="gc-term-layout-hint">勾选 2-4 个 pane（第一个为基准）。下方列表仅服务组合，拆开整组不看勾选。</span>
            </div>
            <div className="gc-term-layout-list" data-testid="herdr-terminal-layout-list">
              {sortComposePanes(layoutPanes).map((pane) => {
                const checked = selectedIds.includes(pane.pane_id)
                const isAgent = Boolean(pane.agent)
                const focusMark = layoutTarget?.pane_id === pane.pane_id ? ' · 当前焦点' : ''
                return (
                  <label
                    key={pane.pane_id}
                    className={`gc-term-layout-pane${checked ? ' is-checked' : ''}`}
                  >
                    <input
                      type="checkbox"
                      value={pane.pane_id}
                      checked={checked}
                      onChange={() => togglePick(pane.pane_id)}
                    />
                    <span>
                      <b>{isAgent ? paneComposeLabel(pane) : '(空 shell)'}</b>
                      {!isAgent && <span className="gc-term-layout-tag">空 shell</span>}
                      <span className="gc-term-layout-meta">{focusMark}</span>
                      <br />
                      <span className="gc-term-layout-meta">
                        {pane.pane_id}
                        {pane.tab_id ? ` · ${pane.tab_id}` : ''}
                      </span>
                    </span>
                  </label>
                )
              })}
            </div>
            {layoutNote && <span className="gc-term-layout-note">{layoutNote}</span>}
          </div>
        )}
        {clipboardNote && (
          <div className="gc-term-clipboard-note" role="status">
            {clipboardNote}
          </div>
        )}
        {uploadNote && (
          <div className="gc-term-upload-note" role="status">
            {uploadNote}
          </div>
        )}
        {fileNote && <div className="gc-term-file-note">{fileNote}</div>}
        <div className="gc-herdr-terminal-main">
          <div className="gc-herdr-terminal-stage">
            <div ref={hostRef} className="gc-herdr-terminal-surface" data-testid="herdr-terminal-surface">
              {loading && !error && (
                <div className="gc-herdr-terminal-loading" role="status">
                  正在加载历史输出…
                </div>
              )}
            </div>
            {touch && inputOpen && (
              <form
                className="gc-term-inline-input"
                onSubmit={(event) => {
                  event.preventDefault()
                  sendInline()
                }}
              >
                <input
                  type="text"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter') return
                    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
                    event.preventDefault()
                    sendInline()
                  }}
                  placeholder="输入发给 agent 的文字…"
                  enterKeyHint="send"
                  autoComplete="off"
                  disabled={loading || Boolean(error)}
                  aria-label="终端文字输入"
                  data-testid="herdr-terminal-inline-input"
                />
                <button type="submit" disabled={loading || Boolean(error) || !draft.trim()}>
                  发送
                </button>
              </form>
            )}
            {error && <div className="gc-modal-error">{error}</div>}
          </div>
          {filesOpen && fileRoot && (
            <HerdrTerminalFilePanel
              key={`${session}:${fileRoot}`}
              session={session}
              root={fileRoot}
              onClose={() => setFilesOpen(false)}
            />
          )}
        </div>
      </div>
    </div>
  )
}
