import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import type { Project, Workspace } from '../api/types'
import { ApiError } from '../api/client'
import {
  STREAM_CLOSE,
  closeTerminalTicket,
  connectTerminalStream,
  createTerminalTicket,
  getTerminalTicket,
  interruptTerminalTicket,
  listTerminalTickets,
  newIdempotencyKey,
  reconnectTerminalTicket,
  restartTerminalTicket,
  type TerminalStream,
  type TerminalTicketView,
} from '../api/terminals'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { capability, useCapability, workspaceScope } from '../state/capabilities'

const XTERM_THEME = {
  background: '#0c121b',
  foreground: '#cdd7e6',
  cursor: '#cdd7e6',
  blue: '#77bdf0',
  green: '#61c997',
  yellow: '#e3b469',
  brightBlack: '#66788f',
}

const FALLBACK_DIMS = { cols: 80, rows: 24 }

/** 冻结 WS 状态词（合同审计 §6） */
type StreamPhase =
  | 'attaching'
  | 'replaying'
  | 'live'
  | 'reconnecting'
  | 'exited'
  | 'process_unknown'
  | 'error'
  | 'stopped'
  | 'detached' // 视图 tab 已关闭/未接管：零 WS、零 POST、PTY 保持

interface TabSession {
  view: TerminalTicketView
  phase: StreamPhase
  error: string | null
  truncated: boolean
  open: boolean
}

interface TerminalHandle {
  term: Terminal
  fit: FitAddon
}

/** 不可用（capability fail-closed）：保持只读外壳，零 POST/WS */
function UnavailableBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const ptyCap = useCapability(
    'terminal.pty',
    workspaceScope(project.slug ?? '', workspace.id ?? ''),
  )

  useEffect(() => {
    if (!containerRef.current) return
    const term = new Terminal({
      fontSize: 11,
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      cursorBlink: false,
      disableStdin: true,
      theme: XTERM_THEME,
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    fit.fit()
    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      term.dispose()
    }
  }, [])

  const btnTitle = (action: string) => `${ptyCap.reason ?? 'PTY 未接通'}（${action}不可用）`

  return (
    <>
      <PageHeader
        title="终端"
        sub={workspace.name ?? workspace.id}
        actions={
          <>
            <Button variant="secondary" disabled title={btnTitle('中断')}>
              中断
            </Button>
            <Button variant="secondary" disabled title={btnTitle('重连')}>
              重连
            </Button>
            <Button variant="danger" disabled title={btnTitle('重启')}>
              重启
            </Button>
          </>
        }
      />
      <StatusState
        kind="disconnected"
        banner
        title="PTY 未接通"
        description={ptyCap.reason ?? '服务端未声明该 Workspace 的终端能力。'}
        docsRoute={ptyCap.docsRoute}
      />
      <div ref={containerRef} className="terminal-surface" data-testid="terminal-surface" />
    </>
  )
}

/** 单个 tab 的 xterm surface（挂载一次；未选中仅隐藏，不销毁输出） */
function TabSurface({
  ticketId,
  visible,
  onMount,
  onInput,
}: {
  ticketId: string
  visible: boolean
  onMount: (ticketId: string, handle: TerminalHandle | null) => void
  onInput: (ticketId: string, value: string) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current) return
    const term = new Terminal({
      fontSize: 11,
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      cursorBlink: true,
      disableStdin: false,
      theme: XTERM_THEME,
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    fit.fit()
    onMount(ticketId, { term, fit })
    const sub = term.onData((value) => onInput(ticketId, value))
    return () => {
      sub.dispose()
      onMount(ticketId, null)
      term.dispose()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticketId])

  // 重新可见时的 fit 由父级选中/resize 逻辑触发（隐藏期间 proposeDimensions 不可用）
  return (
    <div
      ref={containerRef}
      className={`terminal-surface${visible ? '' : ' terminal-surface--hidden'}`}
      data-testid={`terminal-surface-${ticketId}`}
    />
  )
}

function LiveBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const controlUi = capability('terminal.control.ui')

  const [listPhase, setListPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [listError, setListError] = useState<string | null>(null)
  const [tickets, setTickets] = useState<TerminalTicketView[]>([])
  const [tabs, setTabs] = useState<Record<string, TabSession>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmingClose, setConfirmingClose] = useState(false)
  const [createPending, setCreatePending] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [copied, setCopied] = useState(false)

  const tabsRef = useRef<Record<string, TabSession>>({})
  interface StreamEntry {
    stream: TerminalStream
    fence: { revision: number; generation: number; cursor: number }
  }
  const streamsRef = useRef(new Map<string, StreamEntry>())
  const termsRef = useRef(new Map<string, TerminalHandle>())
  const intentKeysRef = useRef<Record<string, string>>({})
  const selectedRef = useRef<string | null>(null)

  const projectId = project.project_id ?? ''
  const workspaceId = workspace.workspace_id ?? workspace.id ?? ''

  const syncTabs = useCallback((next: Record<string, TabSession>) => {
    tabsRef.current = next
    setTabs(next)
  }, [])

  const patchTab = useCallback(
    (ticketId: string, patch: Partial<TabSession>) => {
      const current = tabsRef.current[ticketId]
      if (!current) return
      syncTabs({ ...tabsRef.current, [ticketId]: { ...current, ...patch } })
    },
    [syncTabs],
  )

  const syncSelected = useCallback((next: string | null) => {
    selectedRef.current = next
    setSelected(next)
  }, [])

  const dims = useCallback(() => {
    const id = selectedRef.current
    const handle = id ? termsRef.current.get(id) : undefined
    const proposed = handle?.fit.proposeDimensions()
    if (proposed && proposed.cols > 0 && proposed.rows > 0) {
      return { cols: proposed.cols, rows: proposed.rows }
    }
    return FALLBACK_DIMS
  }, [])

  /** 同一用户 intent 的幂等键在重试间复用；成功或换新 intent 即作废 */
  const intentKey = useCallback((action: string) => {
    return (intentKeysRef.current[action] ??= newIdempotencyKey())
  }, [])

  const clearIntent = useCallback((action: string) => {
    delete intentKeysRef.current[action]
  }, [])

  const closeStream = useCallback((ticketId: string) => {
    streamsRef.current.get(ticketId)?.stream.close()
    streamsRef.current.delete(ticketId)
  }, [])

  const attach = useCallback(
    (ticketId: string, view: TerminalTicketView) => {
      const handle = termsRef.current.get(ticketId)
      if (!handle) return
      closeStream(ticketId)
      patchTab(ticketId, { view, phase: 'attaching', error: null })
      const fence = {
        revision: view.ticket.revision,
        generation: view.ticket.engine_generation,
        cursor: view.ticket.reconnect_cursor,
      }
      const entry = {} as StreamEntry
      entry.fence = fence
      streamsRef.current.set(ticketId, entry)
      const stream = connectTerminalStream(
        { projectId, workspaceId, ticketId },
        fence,
        {
          onReplayStart: () => {
            handle.term.reset()
            patchTab(ticketId, { phase: 'replaying' })
          },
          onData: (data) => {
            handle.term.write(data)
          },
          onReplayComplete: (truncated) => {
            patchTab(ticketId, { phase: 'live', truncated })
          },
          onExit: () => {
            patchTab(ticketId, { phase: 'exited' })
          },
          onError: (code) => {
            patchTab(ticketId, { error: `终端流错误：${code}` })
          },
          onClose: (code, reason) => {
            // 只响应当前 stream 的关闭；被接管/已替换的旧连接事件直接忽略
            if (streamsRef.current.get(ticketId) !== entry) return
            streamsRef.current.delete(ticketId)
            const current = tabsRef.current[ticketId]
            if (!current || !current.open) return
            if (current.phase === 'stopped' || current.phase === 'detached') return
            if (current.phase === 'exited') return // exit 帧后的 4409 是自然退出收尾
            if (code === STREAM_CLOSE.CONFLICT || code === STREAM_CLOSE.UNAVAILABLE || code === 1006) {
              // stale/taken-over/stopped/authority-I/O：先 refetch 权威 projection，不盲重连
              patchTab(ticketId, { phase: 'reconnecting', error: reason || `终端流断开（${code}）` })
              getTerminalTicket(projectId, workspaceId, ticketId)
                .then((fresh) => {
                  const tab = tabsRef.current[ticketId]
                  if (!tab || !tab.open || tab.phase !== 'reconnecting') return
                  if (fresh.runtime.state === 'running') {
                    patchTab(ticketId, { view: fresh })
                  } else if (fresh.runtime.state === 'exited') {
                    patchTab(ticketId, { view: fresh, phase: 'exited' })
                  } else if (fresh.runtime.state === 'process_unknown') {
                    patchTab(ticketId, { view: fresh, phase: 'process_unknown' })
                  } else {
                    patchTab(ticketId, { view: fresh, phase: 'stopped' })
                  }
                })
                .catch(() => {
                  /* 保留 reconnecting，由用户显式重试 */
                })
              return
            }
            if (code === STREAM_CLOSE.UNAUTHORIZED) {
              patchTab(ticketId, { phase: 'error', error: '终端流未通过服务端信任校验（1008）。' })
              return
            }
            if (code === STREAM_CLOSE.NOT_FOUND) {
              patchTab(ticketId, { phase: 'error', error: '终端 ticket 不存在或已被移除（4404）。' })
              return
            }
            patchTab(ticketId, { phase: 'reconnecting', error: reason || `终端流断开（${code}）` })
          },
        },
      )
      entry.stream = stream
    },
    [closeStream, patchTab, projectId, workspaceId],
  )

  /** 打开（或接管）一个 ticket 的视图 tab */
  const openTab = useCallback(
    (view: TerminalTicketView, select = true) => {
      const existing = tabsRef.current[view.ticket.ticket_id]
      if (select) syncSelected(view.ticket.ticket_id)
      if (existing?.open) return
      syncTabs({
        ...tabsRef.current,
        [view.ticket.ticket_id]: {
          view,
          phase: view.runtime.state === 'running' ? 'attaching' : view.runtime.state === 'exited' ? 'exited' : view.runtime.state === 'process_unknown' ? 'process_unknown' : 'stopped',
          error: null,
          truncated: false,
          open: true,
        },
      })
      // surface 挂载在下一渲染周期；attach 延迟到 mount 回调后由 effect 触发
    },
    [syncSelected, syncTabs],
  )

  const load = useCallback(async () => {
    setListPhase('loading')
    setListError(null)
    try {
      const list = await listTerminalTickets(projectId, workspaceId)
      setTickets(list.items)
      setListPhase('ready')
      if (list.items.length > 0) {
        const first = list.items[0]
        if (Object.keys(tabsRef.current).length === 0) openTab(first)
      }
    } catch (err) {
      setListError(err instanceof ApiError ? `${err.message}（${err.code}）` : '终端列表加载失败')
      setListPhase('error')
    }
  }, [openTab, projectId, workspaceId])

  useEffect(() => {
    void load()
    return () => {
      for (const id of [...streamsRef.current.keys()]) closeStream(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** surface 挂载后，若 tab 处于 attaching 且尚无 stream，则发起 attach */
  const onSurfaceMount = useCallback(
    (ticketId: string, handle: TerminalHandle | null) => {
      if (handle) {
        termsRef.current.set(ticketId, handle)
        const tab = tabsRef.current[ticketId]
        if (tab && tab.open && tab.phase === 'attaching' && !streamsRef.current.has(ticketId)) {
          attach(ticketId, tab.view)
        }
      } else {
        termsRef.current.delete(ticketId)
      }
    },
    [attach],
  )

  const fenceCurrent = (ticketId: string): StreamEntry | null => {
    const tab = tabsRef.current[ticketId]
    const entry = streamsRef.current.get(ticketId)
    if (!tab || !entry) return null
    if (
      entry.fence.revision !== tab.view.ticket.revision ||
      entry.fence.generation !== tab.view.ticket.engine_generation
    ) {
      return null // 控制操作推进 fence 后，旧 WS 帧不再允许副作用
    }
    return entry
  }

  const onSurfaceInput = useCallback((ticketId: string, value: string) => {
    const tab = tabsRef.current[ticketId]
    if (!tab || tab.phase !== 'live') return // replay 完成前禁止 stdin 副作用
    fenceCurrent(ticketId)?.stream.sendInput(value)
  }, [])

  /** 关闭视图 tab：只断开本页 WS，零 POST，不杀 PTY */
  const onCloseViewTab = useCallback(
    (ticketId: string) => {
      closeStream(ticketId)
      const next = { ...tabsRef.current }
      delete next[ticketId]
      syncTabs(next)
      if (selectedRef.current === ticketId) {
        const remaining = Object.keys(next)
        syncSelected(remaining[0] ?? null)
      }
      setConfirmingClose(false)
    },
    [closeStream, syncSelected, syncTabs],
  )

  const runControl = useCallback(
    async (ticketId: string, action: string, call: () => Promise<TerminalTicketView>, after: (next: TerminalTicketView) => void) => {
      patchTab(ticketId, { error: null })
      try {
        const next = await call()
        clearIntent(action)
        patchTab(ticketId, { view: next })
        setTickets((prev) => prev.map((item) => (item.ticket.ticket_id === ticketId ? next : item)))
        after(next)
      } catch (err) {
        patchTab(ticketId, {
          phase: 'error',
          error: err instanceof ApiError ? `${err.message}（${err.code}）` : '终端操作失败',
        })
      }
    },
    [clearIntent, patchTab],
  )

  const selectedTab = selected ? tabs[selected] : undefined
  const selectedTicketId = selectedTab?.view.ticket.ticket_id ?? null

  const onNewTerminal = () => {
    const size = dims()
    const action = 'create'
    setCreatePending(true)
    const key = intentKey(action)
    createTerminalTicket(projectId, workspaceId, workspace.version ?? 1, size.cols, size.rows, key)
      .then((view) => {
        clearIntent(action)
        setCreatePending(false)
        setTickets((prev) => [...prev.filter((item) => item.ticket.ticket_id !== view.ticket.ticket_id), view])
        openTab(view)
      })
      .catch((err) => {
        setCreatePending(false)
        setListError(err instanceof ApiError ? `${err.message}（${err.code}）` : '创建终端失败')
        setListPhase('error')
      })
  }

  const onInterrupt = () => {
    if (!selectedTab || !selectedTicketId) return
    const fence = {
      revision: selectedTab.view.ticket.revision,
      generation: selectedTab.view.ticket.engine_generation,
    }
    void runControl(
      selectedTicketId,
      `interrupt:${selectedTicketId}`,
      () => interruptTerminalTicket({ projectId, workspaceId, ticketId: selectedTicketId }, fence, intentKey(`interrupt:${selectedTicketId}`)),
      (next) => attach(selectedTicketId, next),
    )
  }

  const onReconnect = () => {
    if (!selectedTab || !selectedTicketId) return
    const size = dims()
    void runControl(
      selectedTicketId,
      `reconnect:${selectedTicketId}`,
      () =>
        reconnectTerminalTicket(
          { projectId, workspaceId, ticketId: selectedTicketId },
          {
            revision: selectedTab.view.ticket.revision,
            generation: selectedTab.view.ticket.engine_generation,
            cursor: selectedTab.view.ticket.reconnect_cursor,
          },
          size,
        ),
      (next) => attach(selectedTicketId, next),
    )
  }

  const onRestart = () => {
    if (!selectedTab || !selectedTicketId) return
    const size = dims()
    void runControl(
      selectedTicketId,
      `restart:${selectedTicketId}`,
      () =>
        restartTerminalTicket(
          { projectId, workspaceId, ticketId: selectedTicketId },
          {
            revision: selectedTab.view.ticket.revision,
            generation: selectedTab.view.ticket.engine_generation,
          },
          size,
          intentKey(`restart:${selectedTicketId}`),
        ),
      (next) => attach(selectedTicketId, next),
    )
  }

  /** 关闭会话（真 kill）：显式确认后才 POST /close，恰好一次 fenced 请求 */
  const onCloseSession = () => {
    if (!selectedTab || !selectedTicketId) return
    if (!confirmingClose) {
      setConfirmingClose(true)
      return
    }
    setConfirmingClose(false)
    void runControl(
      selectedTicketId,
      `close:${selectedTicketId}`,
      () =>
        closeTerminalTicket(
          { projectId, workspaceId, ticketId: selectedTicketId },
          {
            revision: selectedTab.view.ticket.revision,
            generation: selectedTab.view.ticket.engine_generation,
          },
          intentKey(`close:${selectedTicketId}`),
        ),
      () => {
        closeStream(selectedTicketId)
        patchTab(selectedTicketId, { phase: 'stopped' })
      },
    )
  }

  const onCopyIdentity = () => {
    if (!selectedTab) return
    const t = selectedTab.view.ticket
    // 只复制可公开 identity：project/workspace/ticket opaque ID；绝不包含内部 term ID/path/cwd/PID/Herdr
    const text = `project=${t.project_id} workspace=${t.workspace_id} ticket=${t.ticket_id}`
    const clipboard = navigator.clipboard
    if (clipboard && typeof clipboard.writeText === 'function') {
      clipboard.writeText(text).then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      })
    }
  }

  const onFullscreen = () => {
    setFullscreen((prev) => !prev)
    requestAnimationFrame(() => {
      const id = selectedRef.current
      const handle = id ? termsRef.current.get(id) : undefined
      handle?.fit.fit()
    })
  }

  // 选中 tab 切换后补一次 fit（隐藏期间尺寸不可读）
  useEffect(() => {
    if (!selected) return
    termsRef.current.get(selected)?.fit.fit()
  }, [selected])

  useEffect(() => {
    const onResize = () => {
      const id = selectedRef.current
      const handle = id ? termsRef.current.get(id) : undefined
      if (!handle) return
      handle.fit.fit()
      const next = handle.fit.proposeDimensions()
      const tab = id ? tabsRef.current[id] : undefined
      if (next && next.cols > 0 && next.rows > 0 && tab?.phase === 'live' && id) {
        fenceCurrent(id)?.stream.sendResize(next.cols, next.rows)
      }
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const running = selectedTab?.view.runtime.state === 'running'
  const controlsBase = controlUi.available && running
  const canInterrupt = controlsBase && selectedTab?.phase === 'live'
  const canReconnect = selectedTab?.phase === 'reconnecting'
  const canRestart =
    selectedTab != null &&
    ['live', 'reconnecting', 'exited', 'process_unknown', 'stopped'].includes(selectedTab.phase)
  const canCloseSession =
    selectedTab != null && selectedTab.phase !== 'stopped' && selectedTab.view.ticket.desired_state !== 'stopped'

  if (listPhase === 'loading') {
    return (
      <>
        <PageHeader title="终端" sub={workspace.name ?? workspace.id} />
        <StatusState kind="loading" banner title="正在加载终端…" />
      </>
    )
  }
  if (listPhase === 'error') {
    return (
      <>
        <PageHeader title="终端" sub={workspace.name ?? workspace.id} />
        <StatusState kind="error" banner title="终端不可用" description={listError ?? '未知错误'}>
          <div className="state-actions">
            <Button variant="primary" onClick={() => void load()}>
              重试
            </Button>
          </div>
        </StatusState>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="终端"
        sub={workspace.name ?? workspace.id}
        actions={
          <>
            <Button variant="primary" disabled={createPending} title={createPending ? '创建进行中' : '创建新终端会话'} onClick={onNewTerminal}>
              新终端
            </Button>
            <Button variant="secondary" disabled={!canInterrupt} title={canInterrupt ? '发送中断（SIGINT）' : '仅 live 流可中断'} onClick={onInterrupt}>
              中断
            </Button>
            <Button variant="secondary" disabled={!canReconnect} title={canReconnect ? '断线重连并续播' : '仅在断开后可重连'} onClick={onReconnect}>
              重连
            </Button>
            <Button variant="danger" disabled={!canRestart} title={canRestart ? '杀死当前 generation 并重启' : '无可重启的终端'} onClick={onRestart}>
              重启
            </Button>
            <Button variant="secondary" disabled={!selectedTab} title="切换终端区域全屏" onClick={onFullscreen}>
              全屏
            </Button>
            <Button variant="secondary" disabled={!selectedTab} title="复制 project/workspace/ticket 公开 identity" onClick={onCopyIdentity}>
              {copied ? '已复制' : '复制 identity'}
            </Button>
            <Button
              variant="danger"
              disabled={!canCloseSession}
              title={canCloseSession ? '确认后杀死 PTY 并关闭 ticket' : '无可关闭的会话'}
              onClick={onCloseSession}
            >
              关闭会话
            </Button>
          </>
        }
      />
      {confirmingClose && selectedTab && (
        <StatusState
          kind="conflict"
          banner
          title="确认关闭会话？"
          description="该操作会终止真实 PTY 进程并把 ticket 置为 stopped（恰好一次 POST /close）。仅关闭视图请用 tab 上的「关闭标签页」。"
        >
          <div className="state-actions">
            <Button variant="danger" onClick={onCloseSession}>
              关闭会话
            </Button>
            <Button variant="secondary" onClick={() => setConfirmingClose(false)}>
              取消
            </Button>
          </div>
        </StatusState>
      )}
      {tickets.length === 0 ? (
        <StatusState
          kind="empty"
          banner
          title="该 Workspace 还没有终端"
          description="创建一个真实 Workspace 终端（/bin/bash 交互式 PTY，身份由服务端 authority 决定）。"
        >
          <div className="state-actions">
            <Button variant="primary" disabled={createPending} onClick={onNewTerminal}>
              新终端
            </Button>
          </div>
        </StatusState>
      ) : (
        <>
          <div className="terminal-tabs" data-testid="terminal-tabs" role="tablist">
            {tickets.map((item) => {
              const id = item.ticket.ticket_id
              const tab = tabs[id]
              const isOpen = tab?.open === true
              const isSelected = selected === id && isOpen
              return (
                <div
                  key={id}
                  role="tab"
                  aria-selected={isSelected}
                  data-testid={`terminal-tab-${id}`}
                  className={`terminal-tab${isSelected ? ' terminal-tab--selected' : ''}${isOpen ? '' : ' terminal-tab--detached'}`}
                >
                  <button
                    type="button"
                    className="terminal-tab-label"
                    title={isOpen ? `ticket ${id}` : `ticket ${id}（未连接，点击重新接管）`}
                    onClick={() => openTab(item)}
                  >
                    {id.slice(-6)}
                  </button>
                  {isOpen && (
                    <button
                      type="button"
                      className="terminal-tab-close"
                      aria-label="关闭标签页"
                      title="关闭标签页（只断开本页连接，不杀 PTY）"
                      onClick={() => onCloseViewTab(id)}
                    >
                      ×
                    </button>
                  )}
                </div>
              )
            })}
          </div>
          {selectedTab && (
            <div className="terminal-runtime-state" data-testid="terminal-runtime-state">
              runtime={selectedTab.view.runtime.state} · 流={selectedTab.phase} · generation={selectedTab.view.ticket.engine_generation} · revision={selectedTab.view.ticket.revision}
            </div>
          )}
          {selectedTab?.phase === 'attaching' && (
            <StatusState kind="loading" banner title="正在连接终端流…" />
          )}
          {selectedTab?.phase === 'replaying' && (
            <StatusState kind="loading" banner title="正在回放终端历史…" />
          )}
          {selectedTab?.phase === 'live' && selectedTab.truncated && (
            <StatusState kind="degraded" banner title="回放历史已截断" description="输出历史超过保留上界，仅回放最近片段。" />
          )}
          {selectedTab?.phase === 'reconnecting' && (
            <StatusState kind="stale" banner title="终端流已断开" description={selectedTab.error ?? '连接被接管或游标过期；重连将从最近游标续播。'} />
          )}
          {selectedTab?.phase === 'exited' && (
            <StatusState kind="degraded" banner title="终端进程已退出" description="进程自然退出并已持久化退出回执；可重启新 generation 或关闭会话。" />
          )}
          {selectedTab?.phase === 'process_unknown' && (
            <StatusState kind="degraded" banner title="终端进程状态未知" description="服务重启后无当前 boot binding；不会自动恢复进程，请显式重启或关闭会话。" />
          )}
          {selectedTab?.phase === 'stopped' && (
            <StatusState kind="empty" banner title="终端会话已停止" description="ticket 已停止；可重启新 generation。" />
          )}
          {selectedTab?.phase === 'error' && (
            <StatusState kind="error" banner title="终端错误" description={selectedTab.error ?? '未知错误'}>
              <div className="state-actions">
                <Button variant="primary" onClick={() => void load()}>
                  重试
                </Button>
              </div>
            </StatusState>
          )}
          <div className={fullscreen ? 'terminal-fullscreen' : undefined}>
            {Object.values(tabs)
              .filter((tab) => tab.open)
              .map((tab) => (
                <TabSurface
                  key={tab.view.ticket.ticket_id}
                  ticketId={tab.view.ticket.ticket_id}
                  visible={selected === tab.view.ticket.ticket_id}
                  onMount={onSurfaceMount}
                  onInput={onSurfaceInput}
                />
              ))}
          </div>
        </>
      )}
    </>
  )
}

function TerminalBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  // 唯一闸门：workspace scope 的 server 权威 terminal.pty（project scope 不得越权开启）
  const ptyCap = useCapability(
    'terminal.pty',
    workspaceScope(project.slug ?? '', workspace.id ?? ''),
  )
  if (!ptyCap.available) return <UnavailableBody project={project} workspace={workspace} />
  return <LiveBody project={project} workspace={workspace} />
}

export function TerminalPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <TerminalBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
