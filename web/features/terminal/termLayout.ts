/** 1.0 layoutPair / layoutCompose / 拆开：从 snapshot 选出当前焦点、一对 Agent、勾选 2-4。 */

export const COMPOSE_MIN_PANES = 2
export const COMPOSE_MAX_PANES = 4

export interface LayoutPane {
  pane_id: string
  session?: string
  agent?: string
  tab_id?: string
  focused?: boolean
  display_name?: string
  mail_name?: string
  label?: string
}

export function panesForSession(panes: LayoutPane[], session: string): LayoutPane[] {
  return panes.filter((pane) => pane.pane_id && (!pane.session || pane.session === session))
}

export function pickLayoutTarget(panes: LayoutPane[], focusedPaneId?: string | null): LayoutPane | null {
  if (focusedPaneId) {
    const hit = panes.find((pane) => pane.pane_id === focusedPaneId)
    if (hit) return hit
  }
  const focused = panes.filter((pane) => pane.focused)
  if (focused.length === 1) return focused[0]
  return panes[0] ?? null
}

export function pickPairIds(panes: LayoutPane[], target: LayoutPane | null): string[] {
  const agents = panes.filter((pane) => pane.agent && pane.pane_id)
  const picks: string[] = []
  if (target?.agent) picks.push(target.pane_id)
  for (const pane of agents) {
    if (!picks.includes(pane.pane_id)) picks.push(pane.pane_id)
    if (picks.length === 2) break
  }
  return picks
}

/** 1.0 layoutFreshContext：焦点 tab 若是单 pane，回退到最大多分屏组。 */
export function layoutGroup(
  target: LayoutPane | null,
  panes: LayoutPane[],
): { tabId: string; group: LayoutPane[] } {
  const byTab = new Map<string, LayoutPane[]>()
  for (const pane of panes) {
    const tabId = pane.tab_id || ''
    if (!tabId) continue
    const list = byTab.get(tabId) || []
    list.push(pane)
    byTab.set(tabId, list)
  }
  let tabId = target?.tab_id || ''
  let group = tabId ? byTab.get(tabId) || [] : []
  if (group.length <= 1) {
    const multi = [...byTab.entries()]
      .filter(([, list]) => list.length > 1)
      .sort((a, b) => b[1].length - a[1].length)
    if (multi.length) {
      const containing = target
        ? multi.find(([, list]) => list.some((pane) => pane.pane_id === target.pane_id))
        : undefined
      const pick = containing || multi[0]
      tabId = pick[0]
      group = pick[1]
    }
  }
  return { tabId, group }
}

export function groupTabId(target: LayoutPane | null, panes: LayoutPane[]): string {
  return layoutGroup(target, panes).tabId
}

/**
 * 1.0 renderLayoutPaneList 默认勾选：
 * 多分屏组勾该组全部 Agent；否则焦点 + 其余 Agent，最多 4 个。
 * 空 shell 不自动勾。
 */
export function defaultComposePicks(
  panes: LayoutPane[],
  target: LayoutPane | null,
  group: LayoutPane[],
): string[] {
  const agents = panes.filter((pane) => pane.agent && pane.pane_id)
  const groupIds = new Set(group.map((pane) => pane.pane_id))
  if (groupIds.size > 1) {
    return agents.filter((pane) => groupIds.has(pane.pane_id)).map((pane) => pane.pane_id)
  }
  const picks: string[] = []
  if (target?.pane_id) picks.push(target.pane_id)
  for (const pane of agents) {
    if (!picks.includes(pane.pane_id)) picks.push(pane.pane_id)
    if (picks.length >= COMPOSE_MAX_PANES) break
  }
  return picks.filter((id) => panes.some((pane) => pane.pane_id === id && pane.agent))
}

export function paneComposeLabel(pane: LayoutPane): string {
  return pane.display_name || pane.mail_name || pane.label || pane.agent || pane.pane_id
}

export function sortComposePanes(panes: LayoutPane[]): LayoutPane[] {
  return [...panes].sort((a, b) => Number(Boolean(b.agent)) - Number(Boolean(a.agent)))
}
