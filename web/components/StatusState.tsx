import type { ReactNode } from 'react'

/**
 * G6 公共状态族。9 类：loading / empty / degraded / disconnected / stale /
 * conflict / forbidden / running / partial-failure，另加兜底 error。
 * 每类有稳定 data-state，供测试与 E2E 断言。
 */
export type StateKind =
  | 'loading'
  | 'empty'
  | 'degraded'
  | 'disconnected'
  | 'stale'
  | 'conflict'
  | 'forbidden'
  | 'running'
  | 'partial-failure'
  | 'error'

type Tone = 'accent' | 'success' | 'warning' | 'danger' | 'neutral'

const TONE_BY_KIND: Record<StateKind, Tone> = {
  loading: 'neutral',
  empty: 'neutral',
  degraded: 'warning',
  disconnected: 'danger',
  stale: 'warning',
  conflict: 'danger',
  forbidden: 'danger',
  running: 'warning',
  'partial-failure': 'warning',
  error: 'danger',
}

const ALERT_KINDS: ReadonlySet<StateKind> = new Set([
  'disconnected',
  'conflict',
  'partial-failure',
  'error',
])

const SPINNER_KINDS: ReadonlySet<StateKind> = new Set(['loading', 'running'])

const DEFAULT_TITLE: Record<StateKind, string> = {
  loading: '加载中…',
  empty: '暂无数据',
  degraded: '部分数据不可用',
  disconnected: '连接已断开',
  stale: '数据可能不是最新',
  conflict: '存在冲突',
  forbidden: '暂不可用',
  running: '操作进行中…',
  'partial-failure': '部分操作失败',
  error: '出错了',
}

const ICON_BY_KIND: Partial<Record<StateKind, string>> = {
  empty: '○',
  forbidden: '!',
  degraded: '!',
  stale: '!',
  conflict: '!',
  disconnected: '×',
  error: '×',
  'partial-failure': '!',
}

export interface StatusAction {
  label: string
  onClick: () => void
}

export interface StatusStateProps {
  kind: StateKind
  title?: string
  description?: string
  /** forbidden/unavailable：真实原因 */
  reason?: string | null
  /** 文档入口（如 #/settings?view=doctor） */
  docsRoute?: string
  docsLabel?: string
  action?: StatusAction
  /** stale：上次更新时间 */
  updatedAt?: string | null
  /** banner 形态（disconnected/stale 等行内提醒），默认居中块 */
  banner?: boolean
  children?: ReactNode
}

export function StatusState({
  kind,
  title,
  description,
  reason,
  docsRoute,
  docsLabel = '查看路线图',
  action,
  updatedAt,
  banner = false,
  children,
}: StatusStateProps) {
  const tone = TONE_BY_KIND[kind]
  const isAlert = ALERT_KINDS.has(kind)
  const icon = ICON_BY_KIND[kind]
  const cls = banner ? `state-banner state-banner--${tone}` : 'state'

  return (
    <div
      className={cls}
      data-state={kind}
      role={isAlert ? 'alert' : 'status'}
      aria-live={isAlert ? undefined : 'polite'}
    >
      {SPINNER_KINDS.has(kind) ? (
        <span className={`state-spinner state-spinner--${tone}`} aria-hidden="true" />
      ) : (
        <span className={`state-icon state-icon--${tone}`} aria-hidden="true">
          {icon}
        </span>
      )}
      <div className="state-body">
        <p className="state-title">{title ?? DEFAULT_TITLE[kind]}</p>
        {description ? <p className="state-desc">{description}</p> : null}
        {reason ? <p className="state-reason">{reason}</p> : null}
        {kind === 'stale' && updatedAt ? (
          <p className="state-desc">上次更新：{updatedAt}</p>
        ) : null}
        {children}
        {action || docsRoute ? (
          <div className="state-actions">
            {action ? (
              <button type="button" className="btn btn--primary" onClick={action.onClick}>
                {action.label}
              </button>
            ) : null}
            {docsRoute ? (
              <a className="btn btn--ghost" href={docsRoute}>
                {docsLabel}
              </a>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}
