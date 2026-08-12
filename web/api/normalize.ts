import type { Attention, AttentionItem, ResponseMeta, SourceMeta, Workspace } from './types'

/** remote 判定：workspace 载荷的 location/runtime 字段 */
export function isRemoteWorkspace(w: Workspace): boolean {
  return (w.location ?? w.runtime ?? '').toLowerCase() === 'remote'
}

/** 宽容提取 attention 列表：支持 {items:[...]}、直接数组、overview.attention 内嵌 */
export function attentionItems(attention: Attention | AttentionItem[] | null | undefined): AttentionItem[] {
  if (!attention) return []
  if (Array.isArray(attention)) return attention
  if (Array.isArray(attention.items)) return attention.items
  return []
}

/** 「需要处理」判定：宽容匹配 status/kind 标记 */
export function isNeedsAction(item: AttentionItem): boolean {
  const s = (item.status ?? '').toLowerCase().replace(/[_\s]/g, '-')
  const k = (item.kind ?? '').toLowerCase()
  return s === 'needs-action' || s === 'needsaction' || s === 'pending' || k === 'review' || k === 'question'
}

export function needsActionCount(attention: Attention | AttentionItem[] | null | undefined): number {
  return attentionItems(attention).filter(isNeedsAction).length
}

/** meta.partial 或 sources 含非 available → degraded */
export function degradedSources(meta: ResponseMeta | null | undefined): SourceMeta[] {
  if (!meta) return []
  return (meta.sources ?? []).filter((s) => s.status != null && s.status !== 'available')
}

export function isDegraded(meta: ResponseMeta | null | undefined): boolean {
  return !!meta && (meta.partial === true || degradedSources(meta).length > 0)
}
