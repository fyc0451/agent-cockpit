import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  approveTeamReplyDraft,
  listTeamReplyDrafts,
  rejectTeamReplyDraft,
  setTeamReplyMode,
} from '../../api/teamAuth'
import type { TeamBinding } from './model'

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error)
}

export function TeamReplyPanel({
  topic,
  binding,
}: {
  topic: string
  binding: TeamBinding
}) {
  const queryClient = useQueryClient()
  const mode = binding.replyMode ?? 'confirm'
  const draftsQ = useQuery({
    queryKey: ['team-reply-drafts', topic],
    queryFn: () => listTeamReplyDrafts(topic),
    enabled: topic.length > 0,
    refetchInterval: 2_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
  })
  const modeM = useMutation({
    mutationFn: (next: 'confirm' | 'auto') => setTeamReplyMode(topic, next),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['team-bindings'] })
    },
  })
  const decisionM = useMutation({
    mutationFn: ({ id, decision }: { id: number; decision: 'approve' | 'reject' }) => (
      decision === 'approve'
        ? approveTeamReplyDraft(topic, id)
        : rejectTeamReplyDraft(topic, id)
    ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['team-reply-drafts', topic] }),
        queryClient.invalidateQueries({ queryKey: ['team-chat', topic] }),
      ])
    },
  })

  const changeMode = (next: 'confirm' | 'auto') => {
    if (next === mode || modeM.isPending || binding.ready !== true) return
    if (
      next === 'auto'
      && !window.confirm('切换为自动回复后，Lead 可以不经逐条确认直接对外回复。确认启用？')
    ) return
    modeM.mutate(next)
  }

  const drafts = draftsQ.data ?? []

  return (
    <section className="gc-team-reply-panel" aria-label="团队回复设置">
      <div className="gc-team-reply-head">
        <div>
          <strong>Lead 回复</strong>
          <span className="gc-team-reply-status">
            {mode === 'auto' ? '自动回复' : '需确认回复'}
          </span>
        </div>
        <div className="gc-team-reply-modes" role="group" aria-label="回复模式">
          <button
            type="button"
            aria-pressed={mode === 'confirm'}
            disabled={modeM.isPending || binding.ready !== true}
            onClick={() => changeMode('confirm')}
          >
            需确认
          </button>
          <button
            type="button"
            aria-pressed={mode === 'auto'}
            disabled={modeM.isPending || binding.ready !== true}
            onClick={() => changeMode('auto')}
          >
            自动回复
          </button>
        </div>
      </div>
      {binding.ready !== true && (
        <div className="gc-team-reply-note">绑定的 Lead 当前不可用，暂不能切换模式。</div>
      )}
      {modeM.isError && <div className="gc-team-error">{errorText(modeM.error)}</div>}
      <div className="gc-team-reply-drafts-head">
        待确认草稿{drafts.length > 0 ? `（${drafts.length}）` : ''}
      </div>
      {draftsQ.isError && <div className="gc-team-error">{errorText(draftsQ.error)}</div>}
      {!draftsQ.isPending && !draftsQ.isError && drafts.length === 0 && (
        <div className="gc-team-reply-note">暂无待确认草稿</div>
      )}
      {drafts.length > 0 && (
        <div className="gc-team-reply-drafts">
          {drafts.map((draft) => (
            <article key={draft.id} className="gc-team-reply-draft">
              <div className="gc-team-reply-draft-meta">
                草稿 #{draft.id}
                {draft.mentionHandles.map((handle) => ` · @${handle}`).join('')}
              </div>
              <strong>{draft.subject || '无主题'}</strong>
              <div className="gc-team-reply-draft-body">{draft.body}</div>
              <div className="gc-team-reply-actions">
                <button
                  type="button"
                  disabled={decisionM.isPending}
                  onClick={() => decisionM.mutate({ id: draft.id, decision: 'reject' })}
                >
                  拒绝
                </button>
                <button
                  type="button"
                  className="is-primary"
                  disabled={decisionM.isPending}
                  onClick={() => decisionM.mutate({ id: draft.id, decision: 'approve' })}
                >
                  确认发送
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
      {decisionM.isError && <div className="gc-team-error">{errorText(decisionM.error)}</div>}
    </section>
  )
}
