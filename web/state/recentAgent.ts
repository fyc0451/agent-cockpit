// P1-b 首用恢复：每个持久 project_id/workspace_id 记住最近一次成功创建或恢复的 agent_id。
// 最小浏览器持久状态（localStorage 单键），fail-closed 解析、容量有界；不新增后端 API。

const STORAGE_KEY = 'cockpit.recentAgent.v1'
const MAX_ENTRIES = 50

function scopeKey(projectId: string, workspaceId: string): string {
  return `${projectId}/${workspaceId}`
}

function readAll(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return {}
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof v === 'string' && v !== '') out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function writeAll(map: Record<string, string>): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch {
    /* 隐私模式/配额满：放弃持久化，不阻塞主流程 */
  }
}

export function rememberRecentAgent(projectId: string, workspaceId: string, agentId: string): void {
  if (!projectId || !workspaceId || !agentId) return
  const all = readAll()
  const k = scopeKey(projectId, workspaceId)
  delete all[k] // 重插到末尾 = 最近
  all[k] = agentId
  const keys = Object.keys(all)
  while (keys.length > MAX_ENTRIES) {
    const oldest = keys.shift()
    if (oldest !== undefined) delete all[oldest]
  }
  writeAll(all)
}

export function lookupRecentAgent(projectId: string, workspaceId: string): string | null {
  if (!projectId || !workspaceId) return null
  return readAll()[scopeKey(projectId, workspaceId)] ?? null
}

export function clearRecentAgent(projectId: string, workspaceId: string): void {
  const all = readAll()
  if (delete all[scopeKey(projectId, workspaceId)]) writeAll(all)
}
