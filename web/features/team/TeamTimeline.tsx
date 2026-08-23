// 团队时间线：只读写远端 Team Hub，不进入本机群聊瀑布流。

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { listTeamMessages, sendTeamMessage } from '../../api/teamLedger'
import type { TeamBinding } from './model'
import { TeamReplyPanel } from './TeamReplyPanel'

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
        {rows.map((row) => (
          <div key={row.id} className="gc-team-msg" data-testid={`team-msg-${row.id}`}>
            <div className="gc-team-msg-meta">
              {row.sender_name}
              {row.sender_agent ? ` · via ${row.sender_agent}` : ''}
              {row.mention_handles.map((handle) => ` · @${handle}`).join('')}
            </div>
            {row.subject && row.subject !== '群聊消息' && (
              <div className="gc-team-msg-meta">{row.subject}</div>
            )}
            <div className="gc-team-msg-body">{row.body_md}</div>
          </div>
        ))}
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
    </div>
  )
}
