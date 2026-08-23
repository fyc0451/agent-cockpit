// 团队时间线：只读写远端 Team Hub，不进入本机群聊瀑布流。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  approveTeamReplyRequest,
  listTeamReplyRequests,
  rejectTeamReplyRequest,
} from '../../api/teamAuth'
import { listTeamMessages, sendTeamMessage } from '../../api/teamLedger'
import type { TeamMessage } from '../../api/teamLedger'
import type { TeamBinding } from './model'
import { TeamReplyPanel } from './TeamReplyPanel'

const REPLY_SUBJECT_PREFIX = 'Re: '
const QUESTION_PREVIEW_LENGTH = 20

function questionPreview(text: string): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return '（提问内容为空）'
  const chars = Array.from(normalized)
  return chars.length > QUESTION_PREVIEW_LENGTH
    ? `${chars.slice(0, QUESTION_PREVIEW_LENGTH).join('')}…`
    : normalized
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
}: {
  topic: string
  topicName: string
  binding?: TeamBinding | null
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
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)

  const onSend = async () => {
    const text = draft.trim()
    if (!text || sending) return
    setSending(true)
    setSendError(null)
    try {
      await sendTeamMessage(topic, text)
      setDraft('')
      await queryClient.invalidateQueries({ queryKey: ['team-chat', topic] })
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

  const rows = messagesQ.data ?? []
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
      <div className="gc-team-timeline-list">
        {rows.length === 0 && !messagesQ.isPending && (
          <div className="gc-event">还没有团队消息。发一条试试。</div>
        )}
        {rows.map((row) => {
          const request = replyRequests.get(row.id)
          const question = replyQuestions.get(row.id)
          return (
          <div key={row.id} className="gc-team-msg" data-testid={`team-msg-${row.id}`}>
            <div className="gc-team-msg-meta">
              {row.sender_name}
              {row.sender_agent ? ` · via ${row.sender_agent}` : ''}
              {row.mention_handles.map((handle) => ` · @${handle}`).join('')}
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
        <button type="submit" disabled={sending || !draft.trim()}>
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
