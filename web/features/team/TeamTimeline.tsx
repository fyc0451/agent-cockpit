// 团队时间线：只读写 /api/team/ledger*，不进本机群聊瀑布流。

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { listTeamLedger, sendTeamLedger } from '../../api/teamLedger'

export function TeamTimeline({
  topic,
  topicName,
}: {
  topic: string
  topicName: string
}) {
  const queryClient = useQueryClient()
  const messagesQ = useQuery({
    queryKey: ['team-ledger', topic],
    queryFn: () => listTeamLedger(topic),
    enabled: topic.length > 0,
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
      await sendTeamLedger(topic, text)
      setDraft('')
      await queryClient.invalidateQueries({ queryKey: ['team-ledger', topic] })
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
        团队话题 {topicName}。消息只待团队账本，不进本机群。
      </div>
      {messagesQ.isError && (
        <div className="gc-event">
          团队时间线读失败：
          {messagesQ.error instanceof ApiError
            ? messagesQ.error.message
            : String(messagesQ.error)}
        </div>
      )}
      <div className="gc-team-timeline-list">
        {rows.length === 0 && !messagesQ.isPending && (
          <div className="gc-event">还没有团队消息。发一条试试。</div>
        )}
        {rows.map((row) => (
          <div
            key={row.id}
            className={`gc-team-msg${row.kind === 'me' ? ' is-me' : ''}`}
            data-testid={`team-msg-${row.id}`}
          >
            <div className="gc-team-msg-meta">
              {row.kind === 'me' ? '我' : row.sender}
            </div>
            <div className="gc-team-msg-body">{row.text}</div>
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
