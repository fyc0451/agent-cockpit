// 团队时间线：只读写远端 Team Hub，不进入本机群聊瀑布流。

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  approveTeamReplyRequest,
  listTeamProgress,
  listTeamReplyRequests,
  rejectTeamReplyRequest,
} from '../../api/teamAuth'
import {
  deleteTeamAttachment,
  handoffTeamMessageToLocal,
  listTeamMessages,
  sendTeamMessage,
  teamAttachmentDownloadUrl,
  uploadTeamAttachment,
} from '../../api/teamLedger'
import type { TeamAttachment, TeamMessage } from '../../api/teamLedger'
import { mentionQueryAt } from '../group-chat/model'
import type {
  TeamBinding, TeamConsultCandidate, TeamMember, TeamProgress, TeamTopic,
} from './model'
import { TeamReplyPanel } from './TeamReplyPanel'

const REPLY_SUBJECT_PREFIX = 'Re: '
const QUESTION_PREVIEW_LENGTH = 20
const MAX_TEAM_ATTACHMENTS = 4

function handoffRequestId(): string {
  const bytes = new Uint8Array(8)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

function attachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

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

export function formatTeamDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return ''
  const seconds = Math.floor(milliseconds / 1000)
  if (seconds < 1) return '不到 1 秒'
  const days = Math.floor(seconds / 86_400)
  const hours = Math.floor((seconds % 86_400) / 3_600)
  const minutes = Math.floor((seconds % 3_600) / 60)
  const remainder = seconds % 60
  return [
    days > 0 ? `${days} 天` : '',
    hours > 0 ? `${hours} 小时` : '',
    minutes > 0 ? `${minutes} 分` : '',
    remainder > 0 ? `${remainder} 秒` : '',
  ].filter(Boolean).join(' ')
}

function replyLatency(question: TeamMessage, reply: TeamMessage): number | null {
  const askedAt = parseTeamTimestamp(question.created_ts)
  const repliedAt = parseTeamTimestamp(reply.created_ts)
  if (!askedAt || !repliedAt) return null
  const milliseconds = repliedAt.getTime() - askedAt.getTime()
  return milliseconds >= 0 ? milliseconds : null
}

function fullTeamTimestamp(value: string): string {
  const parsed = parseTeamTimestamp(value)
  if (!parsed) return '时间未知'
  return [
    `${parsed.getFullYear()}-${twoDigits(parsed.getMonth() + 1)}-${twoDigits(parsed.getDate())}`,
    `${twoDigits(parsed.getHours())}:${twoDigits(parsed.getMinutes())}:${twoDigits(parsed.getSeconds())}`,
  ].join(' ')
}

function progressPhaseText(phase: TeamProgress['phase']): string {
  if (phase === 'blocked') return '处理受阻'
  if (phase === 'waiting') return '等待 Agent 开始'
  return '正在处理'
}

function replySourceText(
  source: NonNullable<TeamMessage['replyEvidence']>['answerSource'],
): string {
  if (source === 'local_lead') return '答复来源：本地开发 Agent'
  if (source === 'context_pack') return '答复来源：Context Pack'
  return '答复来源：Team Agent'
}

function progressElapsed(startedAt: string): string {
  const started = parseTeamTimestamp(startedAt)
  if (!started) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function mergeTeamProgressHistory(
  current: Record<number, TeamProgress[]>,
  progressRows: TeamProgress[],
  visibleMessageIds: number[],
): Record<number, TeamProgress[]> {
  const visibleIds = new Set(visibleMessageIds)
  const next: Record<number, TeamProgress[]> = {}
  for (const [messageId, history] of Object.entries(current)) {
    if (visibleIds.has(Number(messageId))) next[Number(messageId)] = history
  }
  for (const progress of progressRows) {
    if (!visibleIds.has(progress.messageId)) continue
    const history = next[progress.messageId] ?? []
    const previous = history[history.length - 1]
    if (
      !previous
      || previous.sequence !== progress.sequence
      || previous.agentName !== progress.agentName
    ) {
      next[progress.messageId] = [...history, progress].slice(-10)
    }
  }
  return next
}

function TeamProgressCard({
  progress,
  history,
  messageId,
}: {
  progress?: TeamProgress
  history: TeamProgress[]
  messageId: number
}) {
  const active = !!progress
  const [open, setOpen] = useState(active)
  const wasActive = useRef(active)
  useEffect(() => {
    if (active !== wasActive.current) {
      setOpen(active)
      wasActive.current = active
    }
  }, [active])
  return (
    <details
      className="gc-team-progress"
      data-testid={`team-progress-${messageId}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className={`gc-team-progress-dot is-${progress?.phase ?? 'done'}`} />
        {progress
          ? `${progress.agentName ?? 'Team Agent'} · ${progressPhaseText(progress.phase)}`
          : '处理过程 · 已完成'}
        {progress && <small>已用时 {progressElapsed(progress.startedAt)}</small>}
      </summary>
      <div className="gc-team-progress-history">
        {history.map((item) => (
          <div key={`${item.agentName ?? 'agent'}-${item.sequence}`}>
            <time title={fullTeamTimestamp(item.updatedAt)}>
              {formatTeamTimestamp(item.updatedAt)}
            </time>
            <span>{item.summary ?? progressPhaseText(item.phase)}</span>
          </div>
        ))}
      </div>
    </details>
  )
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
  consultTargets = [],
  mentionRequest,
  onOpenLocalSession,
}: {
  topic: string
  topicName: string
  binding?: TeamBinding | null
  membership?: TeamTopic['membership']
  members?: TeamMember[]
  consultTargets?: TeamConsultCandidate[]
  mentionRequest?: { topic: string; handle: string; nonce: number } | null
  onOpenLocalSession?: (session: string) => void
}) {
  const queryClient = useQueryClient()
  const messagesQ = useQuery({
    queryKey: ['team-chat', topic],
    queryFn: () => listTeamMessages(topic),
    enabled: topic.length > 0,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  })
  const requestsQ = useQuery({
    queryKey: ['team-reply-requests', topic],
    queryFn: () => listTeamReplyRequests(topic),
    enabled: topic.length > 0 && binding != null,
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  })
  const progressQ = useQuery({
    queryKey: ['team-progress', topic],
    queryFn: () => listTeamProgress(topic),
    enabled: topic.length > 0,
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
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
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null)
  const [activeMentionIndex, setActiveMentionIndex] = useState(0)
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const [attachments, setAttachments] = useState<TeamAttachment[]>([])
  const [uploading, setUploading] = useState(false)
  const uploadRef = useRef<HTMLInputElement>(null)
  const [handoff, setHandoff] = useState<{
    messageId: number
    requestId: string
    targetSession: string
    scope: string
    acceptance: string
  } | null>(null)
  const [handoffBusy, setHandoffBusy] = useState(false)
  const [handoffError, setHandoffError] = useState<string | null>(null)
  const [handoffDone, setHandoffDone] = useState<{
    messageId: number
    targetSession: string
    lead: string
    notified: boolean
  } | null>(null)
  const [olderRows, setOlderRows] = useState<TeamMessage[]>([])
  const [progressHistory, setProgressHistory] = useState<Record<number, TeamProgress[]>>({})
  const [olderCursor, setOlderCursor] = useState<number | null>(null)
  const [olderHasMore, setOlderHasMore] = useState<boolean | null>(null)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const listContentRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const pendingCaretRef = useRef<number | null>(null)
  const nearBottomRef = useRef(true)
  const pendingPrependRef = useRef<{ height: number; top: number } | null>(null)
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
  const selectedHandleSet = new Set(selectedHandles.map((handle) => handle.toLowerCase()))
  const mentionQuery = mention?.query.trim().toLowerCase() ?? ''
  const mentionCandidates = mention
    ? availableMembers.filter((member) => {
      if (selectedHandleSet.has(member.mention_handle.toLowerCase())) return false
      const searchable = `${member.display_name} ${member.mention_handle}`.toLowerCase()
      return searchable.includes(mentionQuery)
    })
    : []
  const showBroadcastMention = !!mention
    && canBroadcast
    && !broadcast
    && ['all', '所有人', 'everyone'].some((token) => token.startsWith(mentionQuery))
  const mentionOptionCount = mentionCandidates.length + Number(showBroadcastMention)

  useEffect(() => {
    setSelectedHandles([])
    setBroadcast(false)
    setMention(null)
    setOlderRows([])
    setOlderCursor(null)
    setOlderHasMore(null)
    setOlderError(null)
    setAttachments([])
    setHandoff(null)
    setHandoffError(null)
    setHandoffDone(null)
    setProgressHistory({})
  }, [topic])

  useEffect(() => {
    const available = new Set(availableHandleKey.split('|').filter(Boolean))
    setSelectedHandles((current) => current.filter(
      (handle) => available.has(handle.toLowerCase()),
    ))
    if (!canBroadcast) setBroadcast(false)
  }, [availableHandleKey, canBroadcast])

  useLayoutEffect(() => {
    const caret = pendingCaretRef.current
    if (caret === null) return
    pendingCaretRef.current = null
    inputRef.current?.focus()
    inputRef.current?.setSelectionRange(caret, caret)
  }, [draft])

  useEffect(() => {
    if (!mentionRequest || mentionRequest.topic !== topic) return
    const member = availableMembers.find(
      (candidate) => candidate.mention_handle.toLowerCase() === mentionRequest.handle.toLowerCase(),
    )
    if (!member) return
    setBroadcast(false)
    setSelectedHandles((current) => (
      current.some((item) => item.toLowerCase() === member.mention_handle.toLowerCase())
        ? current
        : [...current, member.mention_handle]
    ))
    inputRef.current?.focus()
  }, [mentionRequest, topic, availableHandleKey])

  const removeRecipient = (handle: string) => {
    setSelectedHandles((current) => current.filter(
      (item) => item.toLowerCase() !== handle.toLowerCase(),
    ))
  }

  const chooseMention = (handle: string | null) => {
    if (!mention) return
    const input = inputRef.current
    const caret = input?.selectionStart ?? draft.length
    const next = `${draft.slice(0, mention.start)}${draft.slice(caret)}`
    pendingCaretRef.current = mention.start
    setDraft(next)
    if (handle === null) {
      setBroadcast(true)
      setSelectedHandles([])
    } else {
      setBroadcast(false)
      setSelectedHandles((current) => (
        current.some((item) => item.toLowerCase() === handle.toLowerCase())
          ? current
          : [...current, handle]
      ))
    }
    setMention(null)
  }

  const uploadFiles = async (files: File[]) => {
    const slots = Math.max(0, MAX_TEAM_ATTACHMENTS - attachments.length)
    if (slots === 0 || uploading) return
    setUploading(true)
    setSendError(null)
    const uploaded: TeamAttachment[] = []
    try {
      for (const file of files.slice(0, slots)) {
        uploaded.push(await uploadTeamAttachment(topic, file))
      }
      setAttachments((current) => [...current, ...uploaded])
      if (files.length > slots) {
        setSendError(`每条消息最多 ${MAX_TEAM_ATTACHMENTS} 个附件`)
      }
    } catch (err) {
      setAttachments((current) => [...current, ...uploaded])
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setUploading(false)
      if (uploadRef.current) uploadRef.current.value = ''
    }
  }

  const removeAttachment = async (attachment: TeamAttachment) => {
    setSendError(null)
    try {
      await deleteTeamAttachment(topic, attachment.id)
      setAttachments((current) => current.filter((item) => item.id !== attachment.id))
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    }
  }

  const openHandoff = (row: TeamMessage) => {
    const preferred = binding?.consultTarget?.ready
      ? binding.consultTarget.session
      : consultTargets[0]?.session ?? ''
    setHandoff({
      messageId: row.id,
      requestId: handoffRequestId(),
      targetSession: preferred,
      scope: '',
      acceptance: '先复现问题，完成针对性修复与测试，并返回可核对的结果。',
    })
    setHandoffError(null)
    setHandoffDone(null)
  }

  const submitHandoff = async () => {
    if (!handoff || !handoff.targetSession || !handoff.scope.trim() || handoffBusy) return
    setHandoffBusy(true)
    setHandoffError(null)
    try {
      const result = await handoffTeamMessageToLocal(topic, {
        requestId: handoff.requestId,
        messageId: handoff.messageId,
        targetSession: handoff.targetSession,
        scope: handoff.scope.trim(),
        acceptance: handoff.acceptance.trim(),
      })
      setHandoffDone({
        messageId: handoff.messageId,
        targetSession: result.targetSession,
        lead: result.lead,
        notified: result.notified,
      })
      setHandoff(null)
    } catch (err) {
      setHandoffError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setHandoffBusy(false)
    }
  }

  const onSend = async () => {
    const text = draft.trim() || (
      attachments.length > 0
        ? `附件：${attachments.map((item) => item.filename).join('、')}`
        : ''
    )
    if (!text || !hasRecipients || sending || uploading) return
    setSending(true)
    setSendError(null)
    try {
      await sendTeamMessage(
        topic,
        text,
        broadcast ? null : selectedHandles,
        attachments.map((item) => item.id),
      )
      setDraft('')
      setSelectedHandles([])
      setBroadcast(false)
      setMention(null)
      setAttachments([])
      await queryClient.invalidateQueries({ queryKey: ['team-chat', topic] })
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

  const latestPage = messagesQ.data
  const rowsById = new Map<number, TeamMessage>()
  for (const row of [...olderRows, ...(latestPage?.messages ?? [])]) {
    rowsById.set(row.id, row)
  }
  const rows = [...rowsById.values()].sort((left, right) => left.id - right.id)
  const hasOlder = olderHasMore ?? latestPage?.hasMore ?? false
  const nextBeforeId = olderHasMore === null
    ? latestPage?.nextBeforeId ?? null
    : olderCursor
  const historyKey = rows.map((row) => row.id).join('|')
  const replyQuestions = matchReplyQuestions(rows)
  const repliedQuestionIds = new Set(
    [...replyQuestions.values()].map((question) => question.id),
  )
  const replyRequests = new Map(
    (requestsQ.data ?? []).map((request) => [request.messageId, request]),
  )
  const activeProgress = new Map(
    (progressQ.data ?? []).map((progress) => [progress.messageId, progress]),
  )

  useEffect(() => {
    setProgressHistory((current) => mergeTeamProgressHistory(
      current,
      progressQ.data ?? [],
      rows.map((row) => row.id),
    ))
  }, [historyKey, progressQ.data])

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

  const loadOlder = async () => {
    const list = listRef.current
    if (!list || !hasOlder || nextBeforeId === null || loadingOlder) return
    pendingPrependRef.current = { height: list.scrollHeight, top: list.scrollTop }
    nearBottomRef.current = false
    setLoadingOlder(true)
    setOlderError(null)
    try {
      const page = await listTeamMessages(topic, { limit: 80, beforeId: nextBeforeId })
      setOlderRows((current) => {
        const merged = new Map<number, TeamMessage>()
        for (const row of [...page.messages, ...current]) merged.set(row.id, row)
        return [...merged.values()].sort((left, right) => left.id - right.id)
      })
      setOlderCursor(page.nextBeforeId)
      setOlderHasMore(page.hasMore)
    } catch (error) {
      pendingPrependRef.current = null
      setOlderError(error instanceof ApiError ? error.message : String(error))
    } finally {
      setLoadingOlder(false)
    }
  }

  useLayoutEffect(() => {
    nearBottomRef.current = true
    setHasNew(false)
    pinToBottom()
  }, [topic])

  useLayoutEffect(() => {
    const pending = pendingPrependRef.current
    const list = listRef.current
    if (pending && list) {
      pendingPrependRef.current = null
      list.scrollTop = pending.top + (list.scrollHeight - pending.height)
      return
    }
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
          {hasOlder && (
            <button
              type="button"
              className="gc-team-load-older"
              disabled={loadingOlder}
              onClick={() => void loadOlder()}
            >
              {loadingOlder ? '正在加载…' : '加载更早消息'}
            </button>
          )}
          {olderError && <div className="gc-event">加载更早消息失败：{olderError}</div>}
          {rows.length === 0 && !messagesQ.isPending && (
            <div className="gc-event">还没有团队消息。发一条试试。</div>
          )}
          {rows.map((row) => {
            const request = replyRequests.get(row.id)
            const question = replyQuestions.get(row.id)
            const latency = question ? replyLatency(question, row) : null
            const progress = activeProgress.get(row.id)
            const progressRows = progressHistory[row.id] ?? []
            const showCompletedProgress = repliedQuestionIds.has(row.id) && progressRows.length > 0
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
            {row.attachments.length > 0 && (
              <div className="gc-team-message-attachments" aria-label="消息附件">
                {row.attachments.map((attachment) => (
                  <a
                    key={attachment.id}
                    href={teamAttachmentDownloadUrl(topic, attachment.id)}
                    download={attachment.filename}
                    title={`${attachment.filename} · ${attachment.sha256}`}
                  >
                    📎 {attachment.filename} · {attachmentSize(attachment.size)}
                  </a>
                ))}
              </div>
            )}
            {question && latency !== null && (
              <div className="gc-team-reply-evidence" aria-label="回复耗时">
                <span title={[
                  `提问 ${fullTeamTimestamp(question.created_ts)}`,
                  `回复 ${fullTeamTimestamp(row.created_ts)}`,
                ].join(' · ')}>
                  回复耗时 {formatTeamDuration(latency)}
                </span>
              </div>
            )}
            {row.replyEvidence && (
              <div className="gc-team-reply-evidence" aria-label="回复证据">
                <span title={[
                  row.replyEvidence.sha ? `SHA ${row.replyEvidence.sha}` : null,
                  row.replyEvidence.handoffUpdated
                    ? `handoff ${row.replyEvidence.handoffUpdated}`
                    : null,
                ].filter(Boolean).join(' · ')}>
                  {replySourceText(row.replyEvidence.answerSource)}
                </span>
                {row.replyEvidence.contextAvailable && <span>已冻结项目上下文</span>}
              </div>
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
            {(progress || showCompletedProgress) && (
              <TeamProgressCard
                progress={progress}
                history={progressRows}
                messageId={row.id}
              />
            )}
            {binding?.managedRuntime
              && currentHandle
              && row.mention_handles.some(
                (handle) => handle.toLowerCase() === currentHandle,
              ) && (
              <div className="gc-team-local-handoff">
                {handoff?.messageId === row.id ? (
                  <div className="gc-team-local-handoff-form">
                    <strong>交给本地开发会话</strong>
                    <span>
                      请把要做的事情写成明确范围。只有你在这里填写的内容会被投递；
                      Team 原文不会直接进入本地会话。
                    </span>
                    <label>
                      目标会话
                      <select
                        aria-label="本地处理会话"
                        value={handoff.targetSession}
                        disabled={handoffBusy}
                        onChange={(event) => setHandoff({
                          ...handoff,
                          targetSession: event.target.value,
                        })}
                      >
                        <option value="">请选择同项目普通会话</option>
                        {consultTargets.map((target) => (
                          <option key={target.session} value={target.session}>{target.label}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      授权范围
                      <textarea
                        aria-label="本地处理授权范围"
                        value={handoff.scope}
                        disabled={handoffBusy}
                        onChange={(event) => setHandoff({ ...handoff, scope: event.target.value })}
                      />
                    </label>
                    <label>
                      验收标准
                      <textarea
                        aria-label="本地处理验收标准"
                        value={handoff.acceptance}
                        disabled={handoffBusy}
                        onChange={(event) => setHandoff({
                          ...handoff,
                          acceptance: event.target.value,
                        })}
                      />
                    </label>
                    {consultTargets.length === 0 && (
                      <span className="gc-team-error">当前没有同项目、正在运行的普通开发 Lead。</span>
                    )}
                    {handoffError && <span className="gc-team-error">{handoffError}</span>}
                    <div className="gc-team-reply-actions">
                      <button
                        type="button"
                        disabled={handoffBusy}
                        onClick={() => setHandoff(null)}
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        className="is-primary"
                        disabled={
                          handoffBusy
                          || !handoff.targetSession
                          || !handoff.scope.trim()
                        }
                        onClick={() => void submitHandoff()}
                      >
                        {handoffBusy ? '投递中…' : '确认授权并投递'}
                      </button>
                    </div>
                  </div>
                ) : handoffDone?.messageId === row.id ? (
                  <div className="gc-team-local-handoff-done">
                    <span>
                      已交给 {handoffDone.targetSession} · Lead {handoffDone.lead}
                      {handoffDone.notified ? '，已通知处理' : '，请打开会话继续'}
                    </span>
                    {onOpenLocalSession && (
                      <button
                        type="button"
                        onClick={() => onOpenLocalSession(handoffDone.targetSession)}
                      >
                        打开本地会话
                      </button>
                    )}
                  </div>
                ) : (
                  <button type="button" onClick={() => openHandoff(row)}>
                    交给本地会话处理
                  </button>
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
        {broadcast && (
          <button
            type="button"
            aria-label="移除 @all"
            disabled={sending}
            onClick={() => setBroadcast(false)}
          >
            @all ×
          </button>
        )}
        {!broadcast && selectedHandles.map((handle) => (
          <button
            key={handle.toLowerCase()}
            type="button"
            aria-label={`移除 @${handle}`}
            disabled={sending}
            onClick={() => removeRecipient(handle)}
          >
            @{handle} ×
          </button>
        ))}
        {availableMembers.length === 0 && (
          <span className="gc-team-recipients-empty">没有其他活跃成员</span>
        )}
        {!hasRecipients && availableMembers.length > 0 && (
          <span className="gc-team-recipients-empty">输入 @ 搜索，或从右侧成员列表选择</span>
        )}
      </div>
      {attachments.length > 0 && (
        <div className="gc-team-pending-attachments" aria-label="待发送附件">
          {attachments.map((attachment) => (
            <button
              key={attachment.id}
              type="button"
              disabled={sending || uploading}
              aria-label={`移除附件 ${attachment.filename}`}
              onClick={() => void removeAttachment(attachment)}
            >
              📎 {attachment.filename} · {attachmentSize(attachment.size)} ×
            </button>
          ))}
        </div>
      )}
      <form
        className="gc-team-composer"
        onSubmit={(event) => {
          event.preventDefault()
          void onSend()
        }}
      >
        {mention && mentionOptionCount > 0 && (
          <div className="gc-mention gc-team-mention" role="listbox" aria-label="选择团队消息收件人">
            {showBroadcastMention && (
              <button
                type="button"
                role="option"
                aria-selected={activeMentionIndex === 0}
                aria-label="@all"
                className={`gc-mention-item${activeMentionIndex === 0 ? ' is-active' : ''}`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseMention(null)}
              >
                <span>@all</span>
                <span className="gc-mention-kind">全体成员</span>
              </button>
            )}
            {mentionCandidates.map((member, index) => {
              const optionIndex = index + Number(showBroadcastMention)
              return (
                <button
                  key={member.human_id}
                  type="button"
                  role="option"
                  aria-selected={activeMentionIndex === optionIndex}
                  aria-label={`@${member.mention_handle}`}
                  className={`gc-mention-item${activeMentionIndex === optionIndex ? ' is-active' : ''}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseMention(member.mention_handle)}
                >
                  <span>@{member.mention_handle}</span>
                  <span className="gc-mention-kind">{member.display_name}</span>
                </button>
              )
            })}
          </div>
        )}
        <button
          type="button"
          className="gc-team-attach-button"
          disabled={sending || uploading || attachments.length >= MAX_TEAM_ATTACHMENTS}
          onClick={() => uploadRef.current?.click()}
        >
          {uploading ? '上传中…' : '附件'}
        </button>
        <input
          ref={uploadRef}
          type="file"
          multiple
          hidden
          aria-label="选择团队附件"
          accept=".txt,.md,.log,.csv,.json,.pdf,.png,.jpg,.jpeg,.gif,.webp,.zip,.docx,.xlsx,.pptx"
          onChange={(event) => void uploadFiles(Array.from(event.target.files ?? []))}
        />
        <input
          ref={inputRef}
          aria-label="团队消息"
          value={draft}
          onChange={(event) => {
            const value = event.target.value
            setDraft(value)
            const caret = event.currentTarget.selectionStart ?? value.length
            setMention(mentionQueryAt(value, caret))
            setActiveMentionIndex(0)
          }}
          onKeyDown={(event) => {
            if (!mention || mentionOptionCount === 0) return
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setActiveMentionIndex((index) => (index + 1) % mentionOptionCount)
            } else if (event.key === 'ArrowUp') {
              event.preventDefault()
              setActiveMentionIndex((index) => (
                (index - 1 + mentionOptionCount) % mentionOptionCount
              ))
            } else if (event.key === 'Enter' || event.key === 'Tab') {
              event.preventDefault()
              if (showBroadcastMention && activeMentionIndex === 0) chooseMention(null)
              else {
                const candidate = mentionCandidates[
                  activeMentionIndex - Number(showBroadcastMention)
                ]
                if (candidate) chooseMention(candidate.mention_handle)
              }
            } else if (event.key === 'Escape') {
              event.preventDefault()
              setMention(null)
            }
          }}
          disabled={sending || uploading}
          placeholder="输入消息，键入 @ 选择收件人…"
        />
        <button
          type="submit"
          disabled={
            sending
            || uploading
            || (!draft.trim() && attachments.length === 0)
            || !hasRecipients
          }
        >
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
