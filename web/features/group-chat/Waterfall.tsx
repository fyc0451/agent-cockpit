// 主区瀑布流：谁说的（头像+花名+Leader 徽章）、我的消息右对齐、系统事件行、新消息 pill。

import { useEffect, useRef, useState } from 'react'
import { AgentIcon } from './AgentIcon'
import {
  avatarColor,
  canRecallEntry,
  chatDeliveryLabel,
  chatReceiptLabel,
  formatChatClock,
  formatChatDuration,
  isLiveEntryId,
  layoutMessageBlocks,
  messageFoldPreview,
  messageNeedsFold,
  reflowMessageText,
  splitInlineMarks,
  splitMessageParts,
  unreadCountLabel,
  type ChatDelivery,
  type ChatReceipt,
} from './model'

function InlineText({
  text,
  onOpenPath,
}: {
  text: string
  onOpenPath?: (path: string) => void
}) {
  return (
    <>
      {splitInlineMarks(text).map((part, index) => {
        if (part.type === 'code') return <code key={index} className="gc-msg-inline">{part.text}</code>
        if (part.type === 'strong') return <strong key={index}>{part.text}</strong>
        if (part.type === 'path') {
          if (!onOpenPath) return <span key={index} className="gc-msg-path">{part.text}</span>
          return (
            <button
              key={index}
              type="button"
              className="gc-msg-path"
              title={`打开 ${part.text}`}
              onClick={() => onOpenPath(part.text)}
            >
              {part.text}
            </button>
          )
        }
        return <span key={index}>{part.text}</span>
      })}
    </>
  )
}

function LayoutBlocks({
  text,
  onOpenPath,
}: {
  text: string
  onOpenPath?: (path: string) => void
}) {
  return layoutMessageBlocks(text).map((block, index) => {
    if (block.type === 'heading') {
      return <div key={index} className="gc-msg-h"><InlineText text={block.text} onOpenPath={onOpenPath} /></div>
    }
    if (block.type === 'list') {
      return (
        <ul key={index} className="gc-msg-list">
          {block.items.map((item, itemIndex) => (
            <li key={itemIndex}><InlineText text={item} onOpenPath={onOpenPath} /></li>
          ))}
        </ul>
      )
    }
    if (block.type === 'table') {
      return (
        <div key={index} className="gc-msg-table-wrap">
          <table className="gc-msg-table">
            <thead>
              <tr>
                {block.headers.map((cell, cellIndex) => (
                  <th key={cellIndex}><InlineText text={cell} onOpenPath={onOpenPath} /></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}><InlineText text={cell} onOpenPath={onOpenPath} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    if (block.type === 'code') {
      return (
        <pre key={index} className="gc-msg-code">
          <code><InlineText text={block.text} onOpenPath={onOpenPath} /></code>
        </pre>
      )
    }
    return <div key={index} className="gc-msg-text"><InlineText text={block.text} onOpenPath={onOpenPath} /></div>
  })
}

function MessageBody({
  text,
  onOpenPath,
}: {
  text: string
  onOpenPath?: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  const fold = messageNeedsFold(text)
  const shown = fold && !open ? messageFoldPreview(text) : reflowMessageText(text)
  const parts = splitMessageParts(shown)
  if (parts.length === 0) return <div className="gc-msg-body" />
  return (
    <div className={`gc-msg-body${fold && !open ? ' is-folded' : ''}`}>
      {parts.map((part, index) =>
        part.type === 'code' ? (
          <pre key={index} className="gc-msg-code" data-lang={part.lang || undefined}>
            <code><InlineText text={part.text} onOpenPath={onOpenPath} /></code>
          </pre>
        ) : (
          <LayoutBlocks key={index} text={part.text} onOpenPath={onOpenPath} />
        ),
      )}
      {fold && (
        <button
          type="button"
          className="gc-msg-fold"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? '收起' : '展开全文'}
        </button>
      )}
    </div>
  )
}

export type ChatEntry =
  | {
      id: string
      kind: 'me'
      text: string
      to: string[]
      mailTo: string[]
      ts: number
      recalled?: boolean
      delivery?: ChatDelivery
      receipt?: ChatReceipt
    }
  | {
      id: string
      kind: 'agent'
      paneId: string
      name: string
      agentKind: string
      isLeader: boolean
      text: string
      to: string[]
      ts: number
      durationMs?: number
      unread?: number
    }
  | { id: string; kind: 'event'; text: string; ts: number }
  | { id: string; kind: 'error'; text: string; ts: number }

function fmtTime(ts: number): string {
  return formatChatClock(ts)
}

function EntryRow({
  entry,
  onRecall,
  onEdit,
  onOpenAgent,
  onOpenPath,
}: {
  entry: ChatEntry
  onRecall?: (entry: Extract<ChatEntry, { kind: 'me' }>) => void
  onEdit?: (entry: Extract<ChatEntry, { kind: 'me' }>) => void
  onOpenAgent?: (entry: Extract<ChatEntry, { kind: 'agent' }>) => void
  onOpenPath?: (path: string) => void
}) {
  if (entry.kind === 'event') {
    return <div className="gc-event">{entry.text}</div>
  }
  if (entry.kind === 'error') {
    return (
      <div className="gc-msg gc-msg--error">
        <div className="gc-msg-main">
          <MessageBody text={entry.text} onOpenPath={onOpenPath} />
        </div>
      </div>
    )
  }
  if (entry.kind === 'me') {
    return (
      <div className={`gc-msg gc-msg--me${entry.recalled ? ' is-recalled' : ''}`}>
        <div className="gc-msg-main">
          <div className="gc-msg-meta">
            <span className="gc-msg-name">我</span>
            <span className="gc-boss-badge">Boss</span>
            {entry.to.length > 0 && (
              <span className="gc-msg-kind">→ {entry.to.join('、')}</span>
            )}
            {chatDeliveryLabel(entry.delivery) && (
              <span className={`gc-delivery-badge gc-delivery-badge--${entry.delivery}`}>
                {chatDeliveryLabel(entry.delivery)}
              </span>
            )}
            {chatReceiptLabel(entry.receipt) && (
              <span className={`gc-receipt-badge gc-receipt-badge--${entry.receipt}`}>
                {chatReceiptLabel(entry.receipt)}
              </span>
            )}
            <time className="gc-msg-time">{fmtTime(entry.ts)}</time>
          </div>
          <MessageBody text={entry.recalled ? '已撤回' : entry.text} onOpenPath={onOpenPath} />
          {!entry.recalled && canRecallEntry(entry.ts) && onRecall && onEdit && (
            <div className="gc-msg-actions">
              <button type="button" onClick={() => onRecall(entry)}>撤回</button>
              <button type="button" onClick={() => onEdit(entry)}>修改</button>
            </div>
          )}
        </div>
      </div>
    )
  }
  const live = isLiveEntryId(entry.id)
  const peers = entry.to.filter((name) => name !== '我')
  const toPeer = peers.length > 0
  const toMe = entry.to.includes('我')
  return (
    <div className={`gc-msg${/@(boss|我)(\s|$)/i.test(entry.text) ? ' gc-msg--mention' : ''}${live ? ' gc-msg--live' : ''}${toPeer ? ' gc-msg--peer' : ''}`}>
      <span
        className={`gc-msg-avatar gc-member-avatar--${(entry.agentKind || 'agent').toLowerCase()}`}
        style={{ background: avatarColor(entry.agentKind || entry.name) }}
        aria-hidden
      >
        <AgentIcon kind={entry.agentKind || 'agent'} />
      </span>
      <div className="gc-msg-main">
        <div className="gc-msg-meta">
          <span className="gc-msg-name">{entry.name}</span>
          {entry.isLeader && <span className="gc-leader-badge">Leader</span>}
          <span className="gc-msg-kind">{entry.agentKind}</span>
          {entry.to.length > 0 && (
            <span className="gc-msg-kind">→ {entry.to.join('、')}</span>
          )}
          {toPeer && <span className="gc-peer-badge">回成员</span>}
          {toMe && !toPeer && <span className="gc-peer-badge">回我</span>}
          {live && (
            <span className="gc-live-badge">
              {entry.id.startsWith('typing:') ? '处理中' : '现场'}
            </span>
          )}
          {!live && entry.durationMs != null && formatChatDuration(entry.durationMs) && (
            <span className="gc-duration">用时 {formatChatDuration(entry.durationMs)}</span>
          )}
          <time className="gc-msg-time">{live ? '现在' : fmtTime(entry.ts)}</time>
        </div>
        <MessageBody text={entry.text} onOpenPath={onOpenPath} />
        {live && onOpenAgent && (
          <div className="gc-msg-actions">
            <button
              type="button"
              aria-label={
                unreadCountLabel(entry.unread)
                  ? `看现场，${entry.unread} 条未读`
                  : '看现场'
              }
              onClick={() => onOpenAgent(entry)}
            >
              看现场
              {unreadCountLabel(entry.unread) && (
                <span className="gc-unread-badge">{unreadCountLabel(entry.unread)}</span>
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

interface WaterfallProps {
  entries: ChatEntry[]
  hasSession: boolean
  ungrouped?: boolean
  onRecall?: (entry: Extract<ChatEntry, { kind: 'me' }>) => void
  onEdit?: (entry: Extract<ChatEntry, { kind: 'me' }>) => void
  onOpenAgent?: (entry: Extract<ChatEntry, { kind: 'agent' }>) => void
  onOpenPath?: (path: string) => void
}

export function Waterfall({
  entries, hasSession, ungrouped, onRecall, onEdit, onOpenAgent, onOpenPath,
}: WaterfallProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const nearBottomRef = useRef(true)
  const [hasNew, setHasNew] = useState(false)

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    nearBottomRef.current = near
    if (near) setHasNew(false)
  }

  const historyKey = entries.filter((item) => !isLiveEntryId(item.id)).map((item) => item.id).join('|')
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (nearBottomRef.current) {
      el.scrollTop = el.scrollHeight
    } else if (historyKey) {
      setHasNew(true)
    }
  }, [historyKey])
  useEffect(() => {
    const el = scrollRef.current
    if (!el || !nearBottomRef.current) return
    el.scrollTop = el.scrollHeight
  }, [entries])

  const jumpToBottom = () => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    setHasNew(false)
    nearBottomRef.current = true
  }

  return (
    <div className="gc-flow" ref={scrollRef} onScroll={onScroll} aria-live="polite">
      {entries.length === 0 ? (
        <div className="gc-empty">
          <div className="gc-empty-title">
            {!hasSession ? '选择一个会话' : ungrouped ? '未绑定工作区' : '开始群聊'}
          </div>
          <div className="gc-empty-hint">
            {!hasSession
              ? '从左侧选择会话，或点「新会话」创建'
              : ungrouped
                ? '这个 herdr 会话还没有群聊账本，不会显示其他群的消息。把它加进工作区后再聊。'
                : '还没有这个群的邮件记录。发出去的话会留在本机；刷新不应再丢。'}
          </div>
        </div>
      ) : (
        entries.map((e) => (
          <EntryRow
            key={e.id}
            entry={e}
            onRecall={onRecall}
            onEdit={onEdit}
            onOpenAgent={onOpenAgent}
            onOpenPath={onOpenPath}
          />
        ))
      )}
      {hasNew && (
        <button type="button" className="gc-newmsg" onClick={jumpToBottom}>
          ↓ 有新消息
        </button>
      )}
    </div>
  )
}
