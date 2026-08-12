import type { ReactNode } from 'react'

export type TagTone = 'accent' | 'success' | 'warning' | 'danger' | 'purple' | 'neutral'

export function Tag({ tone = 'neutral', children }: { tone?: TagTone; children: ReactNode }) {
  return <span className={`tag tag--${tone}`}>{children}</span>
}

/** 状态文案 → tag 色调：通过/健康→success，进行中/待→warning，危险/失败/冲突→danger */
export function toneForStatus(status: string | undefined): TagTone {
  const s = (status ?? '').toLowerCase()
  if (['ok', 'pass', 'passed', 'success', 'healthy', 'running', 'done', 'available', 'completed'].includes(s))
    return 'success'
  if (['pending', 'waiting', 'in_progress', 'in-progress', 'processing', 'queued', 'warn', 'warning', 'stale'].includes(s))
    return 'warning'
  if (['fail', 'failed', 'error', 'danger', 'blocked', 'conflict', 'critical', 'down'].includes(s))
    return 'danger'
  return 'neutral'
}

/** location：本机 Local→success 绿，远程→purple 紫 */
export function toneForLocation(location: string | undefined): TagTone {
  const l = (location ?? '').toLowerCase()
  if (l === 'local' || l === '本机') return 'success'
  if (l === 'remote' || l === '远程') return 'purple'
  return 'neutral'
}
