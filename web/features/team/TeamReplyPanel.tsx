import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import { setTeamReplyMode } from '../../api/teamAuth'
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
  const modeM = useMutation({
    mutationFn: (next: 'confirm' | 'auto') => setTeamReplyMode(topic, next),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['team-bindings'] })
    },
  })

  const changeMode = (next: 'confirm' | 'auto') => {
    if (next === mode || modeM.isPending || binding.ready !== true) return
    if (
      next === 'auto'
      && !window.confirm('切换后，Lead 会自动读取每条新消息并直接回复。确认启用？')
    ) return
    modeM.mutate(next)
  }

  return (
    <section className="gc-team-reply-panel" aria-label="团队回复设置">
      <div className="gc-team-reply-head">
        <div>
          <strong>Lead 回复规则</strong>
          <span className="gc-team-reply-status">
            {mode === 'auto' ? '收到后自动回复' : '每条消息先问我'}
          </span>
        </div>
        <div className="gc-team-reply-modes" role="group" aria-label="回复模式">
          <button
            type="button"
            aria-pressed={mode === 'confirm'}
            disabled={modeM.isPending || binding.ready !== true}
            onClick={() => changeMode('confirm')}
          >
            每条先确认
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
      <div className="gc-team-reply-note">
        {mode === 'auto'
          ? 'Lead 收到消息后会直接生成并发送回复。'
          : '收到消息后，请在该消息下方决定是否让 Lead 回复；确认前不会生成答案。'}
      </div>
      {binding.ready !== true && (
        <div className="gc-team-reply-note">绑定的 Lead 当前不可用，暂不能切换模式。</div>
      )}
      {modeM.isError && <div className="gc-team-error">{errorText(modeM.error)}</div>}
    </section>
  )
}
