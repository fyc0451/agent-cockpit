// 团队时间线：只读写 /api/team/ledger*，不进本机群聊瀑布流。

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { handTeamLedgerToLeader, listTeamLedger, sendTeamLedger } from '../../api/teamLedger'

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
  const [handingId, setHandingId] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [autoHand, setAutoHand] = useState(false)

  const onSend = async () => {
    const text = draft.trim()
    if (!text || sending) return
    setSending(true)
    setSendError(null)
    try {
      const row = await sendTeamLedger(topic, text)
      if (autoHand && !row.handed_to_leader) {
        await handTeamLedgerToLeader(row.id)
      }
      setDraft('')
      await queryClient.invalidateQueries({ queryKey: ['team-ledger', topic] })
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

  const onHand = async (messageId: string) => {
    if (handingId) return
    setHandingId(messageId)
    setSendError(null)
    try {
      await handTeamLedgerToLeader(messageId)
      await queryClient.invalidateQueries({ queryKey: ['team-ledger', topic] })
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setHandingId(null)
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
              {row.handed_to_leader ? ' · 已交给 leader' : ''}
            </div>
            <div className="gc-team-msg-body">{row.text}</div>
            {!row.handed_to_leader && (
              <button
                type="button"
                className="gc-team-hand"
                data-testid={`team-hand-${row.id}`}
                disabled={handingId === row.id}
                onClick={() => void onHand(row.id)}
              >
                {handingId === row.id ? '提交中…' : '交给 leader'}
              </button>
            )}
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
        <label className="gc-team-auto-hand">
          <input
            type="checkbox"
            checked={autoHand}
            onChange={(event) => setAutoHand(event.target.checked)}
          />
          发送后交给 leader
        </label>
      </form>
      {sendError && <div className="gc-team-error">{sendError}</div>}
    </div>
  )
}
