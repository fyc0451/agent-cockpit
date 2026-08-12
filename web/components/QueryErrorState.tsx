import { ApiError } from '../api/client'
import { stateKindFromError } from '../api/errorState'
import { routeHrefs } from '../app/routes'
import { StatusState } from './StatusState'

/**
 * Query 错误 → 对应 G6 状态组件。
 * 仅 error.retryable === true 时显示重试；forbidden/conflict/protocol_error 等
 * 只显示错误信息（code、message、request_id 若有）+ docs 入口（forbidden 时）。
 */
export function QueryErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const kind = stateKindFromError(error)
  const isApi = error instanceof ApiError
  const message = isApi ? error.message : error instanceof Error ? error.message : '未知错误'
  const code = isApi ? error.code : 'unknown'
  const requestId = isApi ? error.requestId : null
  const canRetry = isApi && error.retryable && onRetry != null

  return (
    <StatusState
      kind={kind}
      description={kind === 'forbidden' ? undefined : message}
      reason={kind === 'forbidden' ? message : undefined}
      docsRoute={kind === 'forbidden' ? routeHrefs.doctor() : undefined}
      action={canRetry ? { label: '重试', onClick: onRetry } : undefined}
      children={
        <p className="state-desc">
          错误码：{code}
          {requestId ? ` · request_id: ${requestId}` : ''}
        </p>
      }
    />
  )
}
