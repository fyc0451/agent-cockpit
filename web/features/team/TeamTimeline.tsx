// 团队时间线：只读写远端 Team Hub，不进入本机群聊瀑布流。

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  approveTeamReplyRequest,
  listTeamReplyRequests,
  rejectTeamReplyRequest,
} from '../../api/teamAuth'
import { listTeamMessages, sendTeamMessage } from '../../api/teamLedger'
import type { TeamMessage } from '../../api/teamLedger'
import type { TeamBinding, TeamMember, TeamTopic } from './model'
import { TeamReplyPanel } from './TeamReplyPanel'

const REPLY_SUBJECT_PREFIX = 'Re: '
const QUESTION_PREVIEW_LENGTH = 20

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 80
}

function questionPreview(text: string): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return '（提问内容为空）'
  const chars = Array.from(normalized)
  return chars.length > QUESTION_PREVIEW_LENGTH
    ? `${chars.slice(0, QUESTION_PREVIEW_LENGTH).join('')}…`
    : normalized
}

function parseTeamTimestamp(value: string): Date | null {
  const normalized = value.trim()
  if (!normalized) return null
  // Hub 的 SQLite 时间是 UTC naive datetime；带时区的 ISO 值则按自身时区解析。
  const sqlite = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?$/.exec(normalized)
  const parsed = sqlite
    ? new Date(Date.UTC(
      Number(sqlite[1]),
      Number(sqlite[2]) - 1,
      Number(sqlite[3]),
      Number(sqlite[4]),
      Number(sqlite[5]),
      Number(sqlite[6]),
      Number(`0.${sqlite[7] ?? '0'}`) * 1000,
    ))
    : new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function twoDigits(value: number): string {
  return String(value).padStart(2, '0')
}

export function formatTeamTimestamp(value: string, now = new Date()): string {
  const parsed = parseTeamTimestamp(value)
  if (!parsed) return '时间未知'
  const time = `${twoDigits(parsed.getHours())}:${twoDigits(parsed.getMinutes())}`
  const sameDay = parsed.getFullYear() === now.getFullYear()
    && parsed.getMonth() === now.getMonth()
    && parsed.getDate() === now.getDate()
  return sameDay
    ? time
    : `${twoDigits(parsed.getMonth() + 1)}-${twoDigits(parsed.getDate())} ${time}`
}

function fullTeamTimestamp(value: string): string {
  const parsed = parseTeamTimestamp(value)
  if (!parsed) return '时间未知'
  return [
    `${parsed.getFullYear()}-${twoDigits(parsed.getMonth() + 1)}-${twoDigits(parsed.getDate())}`,
    `${twoDigits(parsed.getHours())}:${twoDigits(parsed.getMinutes())}:${twoDigits(parsed.getSeconds())}`,
  ].join(' ')
}

function matchReplyQuestions(rows: TeamMessage[]): Map<number, TeamMessage> {
  const questionsBySubject = new Map<string, TeamMessage[]>()
  const matches = new Map<number, TeamMessage>()

  for (const row of [...rows].sort((left, right) => left.id - right.id)) {
    if (row.subject.startsWith(REPLY_SUBJECT_PREFIX)) {
      const subject = row.subject.slice(REPLY_SUBJECT_PREFIX.length)
      const candidates = questionsBySubject.get(subject)
      const question = candidates?.pop()
      if (question) matches.set(row.id, question)
    }

    const candidates = questionsBySubject.get(row.subject) ?? []
    candidates.push(row)
    questionsBySubject.set(row.subject, candidates)
  }

  return matches
}

export function TeamTimeline({
  topic,
  topicName,
  binding,
  membership,
  members = [],
}: {
  topic: string
  topicName: string
  binding?: TeamBinding | null
  membership?: TeamTopic['membership']
  members?: TeamMember[]
}) {
  const queryClient = useQueryClient()
  const messagesQ = useQuery({
    queryKey: ['team-chat', topic],
    queryFn: () => listTeamMessages(topic),
    enabled: topic.length > 0,
    refetchInterval: 2_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
  })
  const requestsQ = useQuery({
    queryKey: ['team-reply-requests', topic],
    queryFn: () => listTeamReplyRequests(topic),
    enabled: topic.length > 0 && binding != null,
    refetchInterval: 2_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
  })
  const decisionM = useMutation({
    mutationFn: ({ inboxItemId, decision }: {
      inboxItemId: number
      decision: 'approve' | 'reject'
    }) => (
      decision === 'approve'
        ? approveTeamReplyRequest(topic, inboxItemId)
        : rejectTeamReplyRequest(topic, inboxItemId)
    ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['team-reply-requests', topic] }),
        queryClient.invalidateQueries({ queryKey: ['team-chat', topic] }),
      ])
    },
  })
  const [draft, setDraft] = useState('')
  const [selectedHandles, setSelectedHandles] = useState<string[]>([])
  const [broadcast, setBroadcast] = useState(false)
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const listContentRef = useRef<HTMLDivElement>(null)
  const nearBottomRef = useRef(true)
  const [hasNew, setHasNew] = useState(false)
  const currentHandle = membership?.mention_handle.toLowerCase() ?? ''
  const canBroadcast = membership?.role === 'admin'
  const availableMembers = members.filter((member) => (
    member.status === 'active'
    && !!member.mention_handle
    && member.mention_handle.toLowerCase() !== currentHandle
  ))
  const availableHandleKey = availableMembers
    .map((member) => member.mention_handle.toLowerCase())
    .sort()
    .join('|')
  const hasRecipients = broadcast || selectedHandles.length > 0

  useEffect(() => {
    setSelectedHandles([])
    setBroadcast(false)
  }, [topic])

  useEffect(() => {
    const available = new Set(availableHandleKey.split('|').filter(Boolean))
    setSelectedHandles((current) => current.filter(
      (handle) => available.has(handle.toLowerCase()),
    ))
    if (!canBroadcast) setBroadcast(false)
  }, [availableHandleKey, canBroadcast])

  const toggleRecipient = (handle: string) => {
    setBroadcast(false)
    setSelectedHandles((current) => (
      current.some((item) => item.toLowerCase() === handle.toLowerCase())
        ? current.filter((item) => item.toLowerCase() !== handle.toLowerCase())
        : [...current, handle]
    ))
  }

  const onSend = async () => {
    const text = draft.trim()
    if (!text || !hasRecipients || sending) return
    setSending(true)
    setSendError(null)
    try {
      await sendTeamMessage(topic, text, broadcast ? null : selectedHandles)
      setDraft('')
      setSelectedHandles([])
      setBroadcast(false)
      await queryClient.invalidateQueries({ queryKey: ['team-chat', topic] })
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

  const rows = messagesQ.data ?? []
  const historyKey = rows.map((row) => row.id).join('|')
  const replyQuestions = matchReplyQuestions(rows)
  const replyRequests = new Map(
    (requestsQ.data ?? []).map((request) => [request.messageId, request]),
  )

  const replyStatusText = (status: string) => {
    if (status === 'awaiting_confirmation' && binding?.replyMode === 'auto') {
      return '自动回复已启用，等待 Lead 处理…'
    }
    if (status === 'queued') return '已允许回复，等待 Lead 处理…'
    if (status === 'processing') return 'Lead 正在生成回复…'
    if (status === 'replied') return 'Lead 已回复'
    if (status === 'ignored') return '已选择不回复'
    return ''
  }

  const pinToBottom = () => {
    const list = listRef.current
    if (list) list.scrollTop = list.scrollHeight
  }

  const onScroll = () => {
    const list = listRef.current
    if (!list) return
    const near = isNearBottom(list)
    nearBottomRef.current = near
    if (near) setHasNew(false)
  }

  useLayoutEffect(() => {
    nearBottomRef.current = true
    setHasNew(false)
    pinToBottom()
  }, [topic])

  useLayoutEffect(() => {
    if (nearBottomRef.current) pinToBottom()
    else if (historyKey) setHasNew(true)
  }, [historyKey])

  useEffect(() => {
    const content = listContentRef.current
    if (!content) return
    const follow = () => {
      if (nearBottomRef.current) pinToBottom()
    }
    let frame = requestAnimationFrame(follow)
    if (typeof ResizeObserver === 'undefined') {
      return () => cancelAnimationFrame(frame)
    }
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(follow)
    })
    observer.observe(content)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [topic])

  const jumpToBottom = () => {
    nearBottomRef.current = true
    setHasNew(false)
    pinToBottom()
  }

  return (
    <div className="gc-team-timeline" data-testid="team-timeline">
      <div className="gc-team-timeline-hint">
        团队话题 {topicName}。消息同步到 Team Hub，不进本机群。
      </div>
      {binding && <TeamReplyPanel topic={topic} binding={binding} />}
      {messagesQ.isError && (
        <div className="gc-event">
          团队时间线读失败：
          {messagesQ.error instanceof ApiError ? messagesQ.error.message : String(messagesQ.error)}
        </div>
      )}
      <div className="gc-team-timeline-list" ref={listRef} onScroll={onScroll}>
        <div className="gc-team-timeline-list-inner" ref={listContentRef}>
          {rows.length === 0 && !messagesQ.isPending && (
            <div className="gc-event">还没有团队消息。发一条试试。</div>
          )}
          {rows.map((row) => {
            const request = replyRequests.get(row.id)
            const question = replyQuestions.get(row.id)
            return (
            <div key={row.id} className="gc-team-msg" data-testid={`team-msg-${row.id}`}>
            <div className="gc-team-msg-meta gc-team-msg-header">
              <span>
                {row.sender_name}
                {row.sender_agent ? ` · via ${row.sender_agent}` : ''}
                {row.mention_handles.map((handle) => ` · @${handle}`).join('')}
              </span>
              <time
                className="gc-team-msg-time"
                dateTime={row.created_ts}
                title={fullTeamTimestamp(row.created_ts)}
                aria-label={`发送时间 ${fullTeamTimestamp(row.created_ts)}`}
              >
                {formatTeamTimestamp(row.created_ts)}
              </time>
            </div>
            {row.subject && row.subject !== '群聊消息' && (
              <div className="gc-team-msg-meta">{row.subject}</div>
            )}
            {question ? (
              <details className="gc-team-reply-detail">
                <summary>{questionPreview(question.body_md)}</summary>
                <div className="gc-team-reply-detail-section">
                  <strong>完整提问</strong>
                  <div>{question.body_md}</div>
                </div>
                <div className="gc-team-reply-detail-section">
                  <strong>回复详情</strong>
                  <div>{row.body_md}</div>
                </div>
              </details>
            ) : (
              <div className="gc-team-msg-body">{row.body_md}</div>
            )}
            {request && (
              <div className="gc-team-reply-request" data-testid={`reply-request-${row.id}`}>
                {request.status === 'awaiting_confirmation'
                  && binding?.replyMode !== 'auto' ? (
                  <>
                    <span>是否让 Lead 回复这条消息？确认前不会生成答案。</span>
                    <div className="gc-team-reply-actions">
                      <button
                        type="button"
                        disabled={decisionM.isPending}
                        onClick={() => decisionM.mutate({
                          inboxItemId: request.inboxItemId,
                          decision: 'reject',
                        })}
                      >
                        不回复
                      </button>
                      <button
                        type="button"
                        className="is-primary"
                        disabled={decisionM.isPending}
                        onClick={() => decisionM.mutate({
                          inboxItemId: request.inboxItemId,
                          decision: 'approve',
                        })}
                      >
                        让 Lead 回复
                      </button>
                    </div>
                  </>
                ) : (
                  <span>{replyStatusText(request.status)}</span>
                )}
              </div>
            )}
            </div>
            )
          })}
        </div>
      </div>
      {hasNew && (
        <button type="button" className="gc-team-new-message" onClick={jumpToBottom}>
          ↓ 有新消息
        </button>
      )}
      <div className="gc-team-recipients" role="group" aria-label="团队消息收件人">
        <span className="gc-team-recipients-label">发送给</span>
        {canBroadcast && (
          <button
            type="button"
            aria-pressed={broadcast}
            disabled={sending || availableMembers.length === 0}
            onClick={() => {
              setBroadcast((current) => !current)
              setSelectedHandles([])
            }}
          >
            @all 全体成员
          </button>
        )}
        {availableMembers.map((member) => {
          const selected = selectedHandles.some(
            (handle) => handle.toLowerCase() === member.mention_handle.toLowerCase(),
          )
          return (
            <button
              key={member.human_id}
              type="button"
              aria-pressed={selected}
              disabled={sending}
              title={member.display_name || `@${member.mention_handle}`}
              onClick={() => toggleRecipient(member.mention_handle)}
            >
              @{member.mention_handle}
            </button>
          )
        })}
        {availableMembers.length === 0 && (
          <span className="gc-team-recipients-empty">没有其他活跃成员</span>
        )}
        {!hasRecipients && availableMembers.length > 0 && (
          <span className="gc-team-recipients-empty">请选择至少一位收件人</span>
        )}
      </div>
      <form
        className="gc-team-composer"
        onSubmit={(event) => {
          event.preventDefault()
          void onSend()
        }}
      >
        <input
          aria-label="团队消息"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={sending}
          placeholder="发到团队时间线…"
        />
        <button type="submit" disabled={sending || !draft.trim() || !hasRecipients}>
          {sending ? '发送中…' : '发送'}
        </button>
      </form>
      {sendError && <div className="gc-team-error">{sendError}</div>}
      {requestsQ.isError && (
        <div className="gc-team-error">
          {requestsQ.error instanceof ApiError ? requestsQ.error.message : String(requestsQ.error)}
        </div>
      )}
      {decisionM.isError && (
        <div className="gc-team-error">
          {decisionM.error instanceof ApiError ? decisionM.error.message : String(decisionM.error)}
        </div>
      )}
    </div>
  )
}
