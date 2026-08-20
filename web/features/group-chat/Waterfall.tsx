// 主区瀑布流：谁说的（头像+花名+Leader 徽章）、我的消息右对齐、系统事件行、新消息 pill。

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
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
  splitReplyPresentation,
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

function RenderParts({
  text,
  onOpenPath,
}: {
  text: string
  onOpenPath?: (path: string) => void
}) {
  return (
    <>
      {splitMessageParts(text).map((part, index) =>
        part.type === 'code' ? (
          <pre key={index} className="gc-msg-code" data-lang={part.lang || undefined}>
            <code><InlineText text={part.text} onOpenPath={onOpenPath} /></code>
          </pre>
        ) : (
          <LayoutBlocks key={index} text={part.text} onOpenPath={onOpenPath} />
        ),
      )}
    </>
  )
}

function MessageBody({
  text,
  preferConclusion,
  onOpenPath,
}: {
  text: string
  preferConclusion?: boolean
  onOpenPath?: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  const split = preferConclusion ? splitReplyPresentation(text) : { lead: text, rest: '' }
  const leadNeedsFold = messageNeedsFold(split.lead)
  const shownLead = leadNeedsFold && !open
    ? messageFoldPreview(split.lead)
    : reflowMessageText(split.lead)
  const hideProcess = Boolean(split.rest) && !open
  const fold = leadNeedsFold || Boolean(split.rest)
  if (!shownLead && !split.rest) return <div className="gc-msg-body" />
  return (
    <div className={`gc-msg-body${leadNeedsFold && !open ? ' is-folded' : ''}`}>
      <RenderParts text={shownLead} onOpenPath={onOpenPath} />
      {hideProcess && (
        <div className="gc-msg-process">过程已收起</div>
      )}
      {!hideProcess && split.rest ? <RenderParts text={split.rest} onOpenPath={onOpenPath} /> : null}
      {fold && (
        <button
          type="button"
          className="gc-msg-fold"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? '收起' : split.rest ? '展开过程' : '展开全文'}
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
      waiting?: boolean
      git?: { files: number; stat: string }
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
            <span className={`gc-live-badge${entry.waiting ? ' is-waiting' : ''}`}>
              {entry.waiting ? '等你输入' : entry.id.startsWith('typing:') ? '处理中' : '现场'}
            </span>
          )}
          {!live && entry.durationMs != null && formatChatDuration(entry.durationMs) && (
            <span className="gc-duration">用时 {formatChatDuration(entry.durationMs)}</span>
          )}
          <time className="gc-msg-time">{live ? '现在' : fmtTime(entry.ts)}</time>
        </div>
        <MessageBody text={entry.text} preferConclusion onOpenPath={onOpenPath} />
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

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

export function Waterfall({
  entries, hasSession, ungrouped, onRecall, onEdit, onOpenAgent, onOpenPath,
}: WaterfallProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const nearBottomRef = useRef(true)
  const [hasNew, setHasNew] = useState(false)

  const pinToBottom = () => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const near = isNearBottom(el)
    nearBottomRef.current = near
    if (near) setHasNew(false)
  }

  const historyKey = entries.filter((item) => !isLiveEntryId(item.id)).map((item) => item.id).join('|')

  // 进页 / 换会话（父级 key 会拆掉本组件）：首屏前就钉在底部，不要先画出顶部再补滚。
  useLayoutEffect(() => {
    nearBottomRef.current = true
    setHasNew(false)
    pinToBottom()
  }, [])

  useLayoutEffect(() => {
    if (nearBottomRef.current) pinToBottom()
    else if (historyKey) setHasNew(true)
  }, [historyKey])

  useLayoutEffect(() => {
    if (nearBottomRef.current) pinToBottom()
  }, [entries])

  // 气泡撑开、字体/图片把高度拉长后继续钉。容器自己的尺寸变了不算内容变高，所以观察内层。
  useEffect(() => {
    const root = scrollRef.current
    const inner = contentRef.current
    if (!root) return
    const follow = () => {
      if (nearBottomRef.current) pinToBottom()
    }
    let raf = requestAnimationFrame(follow)
    if (typeof ResizeObserver === 'undefined') {
      return () => cancelAnimationFrame(raf)
    }
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(follow)
    })
    ro.observe(inner ?? root)
    return () => {
      ro.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [])

  const jumpToBottom = () => {
    nearBottomRef.current = true
    setHasNew(false)
    pinToBottom()
  }

  const empty = entries.length === 0
  return (
    <div className="gc-flow" ref={scrollRef} onScroll={onScroll} aria-live="polite">
      <div className={`gc-flow-inner${empty ? ' is-empty' : ''}`} ref={contentRef}>
        {empty ? (
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
      </div>
      {hasNew && (
        <button type="button" className="gc-newmsg" onClick={jumpToBottom}>
          ↓ 有新消息
        </button>
      )}
    </div>
  )
}
