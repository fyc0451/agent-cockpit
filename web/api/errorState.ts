import { ApiError } from './client'
import type { StateKind } from '../components/StatusState'

/** ApiError → G6 状态组件映射：403→forbidden，409→conflict，stale 码→stale，transport/网络→disconnected */
export function stateKindFromError(err: unknown): StateKind {
  if (err instanceof ApiError) {
    if (err.status === 403 || err.code === 'forbidden') return 'forbidden'
    if (err.status === 409 || err.code.includes('conflict')) return 'conflict'
    if (err.code.includes('stale')) return 'stale'
    if (['transport_lost', 'disconnected', 'offline'].includes(err.code)) return 'disconnected'
  }
  if (err instanceof TypeError) return 'disconnected'
  return 'error'
}
