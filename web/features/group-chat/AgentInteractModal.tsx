// 打开成员的 herdr pane：确认信任/权限、切模型、发斜杠命令。

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { fetchPaneOutput, sendPane } from '../../api/legacyHerdr'
import { agentEmoji, type ChatMember } from './model'

interface AgentInteractModalProps {
  member: ChatMember
  session: string
  onClose: () => void
}

const PANE_OUTPUT_LIMIT = 64 * 1024
const PANE_WATCH_BUSY_MS = 400
const PANE_WATCH_IDLE_MS = 2000

export function paneWatchInterval(status: string): number {
  return status === 'working' || status === 'blocked' ? PANE_WATCH_BUSY_MS : PANE_WATCH_IDLE_MS
}

export function paneWatchLines(status: string): number {
  return status === 'working' || status === 'blocked' ? 200 : 80
}

function tailOutput(text: string, limit: number): string {
  if (text.length <= limit) return text
  const start = text.length - limit
  const newline = text.indexOf('\n', start)
  return newline >= 0 && newline < text.length - 1
    ? text.slice(newline + 1)
    : text.slice(start)
}

/**
 * herdr pane 接口返回滚动尾窗口；轮询时合并重叠行，而不是用新窗口覆盖旧历史。
 * 无重叠时视为一次全屏重绘，追加为新块；总量有界，避免长时间打开占用无限内存。
 */
export function mergePaneOutput(
  previous: string,
  snapshot: string,
  limit = PANE_OUTPUT_LIMIT,
): string {
  const before = previous.replace(/\r\n/g, '\n')
  const next = snapshot.replace(/\r\n/g, '\n')
  if (!next) return before
  if (!before) return tailOutput(next, limit)
  if (before === next || before.endsWith(next)) return tailOutput(before, limit)
  if (next.startsWith(before)) return tailOutput(next, limit)

  const beforeLines = before.split('\n')
  const nextLines = next.split('\n')
  let overlap = Math.min(beforeLines.length, nextLines.length)
  while (
    overlap > 0 &&
    beforeLines.slice(-overlap).join('\n') !== nextLines.slice(0, overlap).join('\n')
  ) {
    overlap -= 1
  }
  const merged = overlap > 0
    ? [...beforeLines, ...nextLines.slice(overlap)].join('\n')
    : `${before}\n${next}`
  return tailOutput(merged, limit)
}

export function AgentInteractModal({ member, session, onClose }: AgentInteractModalProps) {
  const [output, setOutput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [command, setCommand] = useState('')
  const [busy, setBusy] = useState(false)
  const screenRef = useRef<HTMLPreElement>(null)
  const followTailRef = useRef(true)

  const refresh = async () => {
    try {
      const result = await fetchPaneOutput(session, member.paneId, paneWatchLines(member.status))
      const text = result.output || result.error || '（终端暂无输出）'
      setOutput((current) => mergePaneOutput(current, text))
      setError(result.error)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  useEffect(() => {
    setOutput('')
    setError(null)
    followTailRef.current = true
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, paneWatchInterval(member.status))
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, member.paneId, member.status])

  useLayoutEffect(() => {
    const screen = screenRef.current
    if (screen && followTailRef.current) screen.scrollTop = screen.scrollHeight
  }, [output])

  const send = async (text: string, mode: 'keys' | 'slash' | 'prompt') => {
    if (!text || busy) return
    try {
      await requireAuthenticated()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await sendPane(session, member.paneId, text, mode)
      window.setTimeout(() => { void refresh() }, 250)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const submitCommand = () => {
    const text = command.trim()
    if (!text) return
    setCommand('')
    void send(text, text.startsWith('/') ? 'slash' : 'prompt')
  }

  return (
    <div className="gc-modal-bg" onClick={busy ? undefined : onClose}>
      <div className="gc-modal gc-modal--wide" onClick={(e) => e.stopPropagation()}>
        <h3 className="gc-modal-title">
          {agentEmoji(member.kind)} {member.name}
          {member.isLeader ? ' · Leader' : ''}
        </h3>
        <p className="gc-modal-sub">
          {member.kind}
          {member.status === 'blocked' ? ' · 正在等你确认（信任目录 / 权限 / 提问）' : ' · 直接操作这个 Agent 的终端'}
        </p>
        <pre
          ref={screenRef}
          className="gc-pane-screen"
          role="log"
          aria-label="只读终端现场"
          aria-live="off"
          tabIndex={0}
          onScroll={(event) => {
            const screen = event.currentTarget
            followTailRef.current =
              screen.scrollHeight - screen.scrollTop - screen.clientHeight <= 24
          }}
        >
          {output || '正在读取终端…'}
        </pre>
        {error && <div className="gc-modal-error">{error}</div>}
        <div className="gc-interact-keys">
          <button type="button" className="gc-pill-btn gc-pill-btn--accent" disabled={busy} onClick={() => { void send('y Enter', 'keys') }}>
            确认 Y
          </button>
          <button type="button" className="gc-pill-btn" disabled={busy} onClick={() => { void send('n Enter', 'keys') }}>
            拒绝 N
          </button>
          <button type="button" className="gc-pill-btn" disabled={busy} onClick={() => { void send('Enter', 'keys') }}>
            Enter
          </button>
          <button type="button" className="gc-pill-btn" disabled={busy} onClick={() => { void send('Esc', 'keys') }}>
            Esc
          </button>
        </div>
        <input
          className="gc-input"
          value={command}
          disabled={busy}
          placeholder="斜杠命令，如 /model ；或直接发给 Agent"
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submitCommand()
            }
          }}
        />
        <p className="gc-modal-sub">
          切模型用 <code>/model</code>。Kimi 信任目录点「确认 Y」。权限提示同样用确认/拒绝。
        </p>
        <div className="gc-modal-actions">
          <button type="button" className="gc-pill-btn" onClick={onClose} disabled={busy}>
            关闭
          </button>
          <button
            type="button"
            className="gc-pill-btn gc-pill-btn--accent"
            disabled={busy || command.trim() === ''}
            onClick={submitCommand}
          >
            发送
          </button>
        </div>
      </div>
    </div>
  )
}
