// Capability 权威合并层（本车核心红线）：
// - 静态 registry 是 fail-closed fallback（全部 available=false + 真实 reason）；
// - 任何 query 返回的 meta.capabilities 是权威值，按 scope key 存储：
//   'global' / 'p:<slug>' / 'w:<slug>/<wid>'。hooks 上报时携带所属 scope（从 query key 取），
//   同一 scope 的 snapshot 为 replace 语义；离开 scope 即失效（读取只查当前 scope），
//   不存在跨 project 泄漏与全局永久 merge。
// - useCapability(key, scope) 读取顺序：当前 scope 的 server 值 → 静态 fail-closed fallback。
// 页面渲染只能用 useCapability/capability 读，不得按路径/颜色猜能力。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { routeHrefs } from '../app/routes'
import type { ResponseMeta } from '../api/types'

export interface Capability {
  available: boolean
  reason: string | null
  docsRoute?: string
}

const DOCTOR = routeHrefs.doctor()

const staticRegistry = {
  'memory.local': {
    available: false,
    reason: '项目记忆暂未开放，不影响文件与终端的使用',
    docsRoute: DOCTOR,
  },
  'recovery.review': {
    available: false,
    reason: '变更审核暂未开放，不影响文件与终端的使用',
    docsRoute: DOCTOR,
  },
  'activity.feed': {
    available: false,
    reason: '动态暂未开放，不影响其他功能的使用',
    docsRoute: DOCTOR,
  },
  'git.integration': {
    available: false,
    reason: 'Git 集成暂未开放，可在终端中继续使用 Git',
    docsRoute: DOCTOR,
  },
  'editor.embedded': {
    available: false,
    reason: '内嵌编辑器暂未开放，可继续使用文件浏览与终端',
    docsRoute: DOCTOR,
  },
  browser: {
    available: false,
    reason: '内嵌浏览器暂未开放，不影响其他功能的使用',
    docsRoute: DOCTOR,
  },
  automation: {
    available: false,
    reason: '通信暂未开放，不影响文件与终端的使用',
    docsRoute: DOCTOR,
  },
  'terminal.pty': {
    available: false,
    reason: '该工作空间的终端暂不可用；可稍后重试，或联系管理员确认服务配置',
    docsRoute: DOCTOR,
  },
  'terminal.control.ui': {
    // TERM-003 本地实现开关：client/WS 状态机/controls 已完成，允许为 true；
    // 真实可用性仍由 workspace scope 的 server terminal.pty 闸门（见 TerminalPage）
    available: true,
    reason: null,
  },
  'search.server': {
    available: false,
    reason: '搜索暂未接通，可继续用页面导航查找',
    docsRoute: DOCTOR,
  },
  remoteHerdr: {
    available: false,
    reason: '远程控制暂未接通，远程工作空间暂不可用',
    docsRoute: DOCTOR,
  },
  harnessCatalog: {
    available: false,
    reason: '环境自检目录暂未接通，可稍后重试',
    docsRoute: DOCTOR,
  },
  'projectRegistry.write': {
    available: false,
    reason: '暂时无法添加项目，请稍后重试',
    docsRoute: DOCTOR,
  },
  'files.read': {
    available: false,
    reason: '文件浏览暂未接通，不影响终端使用；请稍后重试',
    docsRoute: DOCTOR,
  },
  'workspace.delete': {
    available: false,
    reason: '工作空间删除暂未开放',
    docsRoute: DOCTOR,
  },
  'settings.write': {
    available: false,
    reason: '设置当前为只读，修改暂不能保存',
    docsRoute: DOCTOR,
  },
} satisfies Record<string, Capability>

export type CapabilityKey = keyof typeof staticRegistry

/** 静态 fail-closed 表（非 React 场景兜底） */
export const capabilities: Record<CapabilityKey, Capability> = staticRegistry

/** 静态表读取（非 React 场景）；React 组件请用 useCapability 以获得 server 权威值 */
export function capability(key: CapabilityKey): Capability {
  return staticRegistry[key]
}

const FALLBACK_CLOSED: Capability = {
  available: false,
  reason: '该能力尚未声明，暂不可用',
  docsRoute: DOCTOR,
}

// ---------- scope ----------

export type CapabilityScope =
  | { kind: 'global' }
  | { kind: 'project'; slug: string }
  | { kind: 'workspace'; slug: string; workspaceId: string }

export const GLOBAL_SCOPE: CapabilityScope = { kind: 'global' }

export function projectScope(slug: string): CapabilityScope {
  return { kind: 'project', slug }
}

export function workspaceScope(slug: string, workspaceId: string): CapabilityScope {
  return { kind: 'workspace', slug, workspaceId }
}

export function scopeKey(scope: CapabilityScope): string {
  switch (scope.kind) {
    case 'global':
      return 'global'
    case 'project':
      return `p:${scope.slug}`
    case 'workspace':
      return `w:${scope.slug}/${scope.workspaceId}`
  }
}

// ---------- server 值解析 ----------

const SYNTHESIZED_REASON = '服务端未说明原因，该能力暂不可用'

/** 宽容解析 meta.capabilities：boolean / { available, reason?, docsRoute? }；
 *  available=false 缺 reason 时合成稳定可读 reason，不得为 null */
export function parseServerCapabilities(raw: unknown): Record<string, Capability> {
  if (!raw || typeof raw !== 'object') return {}
  const out: Record<string, Capability> = {}
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'boolean') {
      out[k] = { available: v, reason: v ? null : SYNTHESIZED_REASON }
    } else if (v && typeof v === 'object') {
      const o = v as { available?: unknown; reason?: unknown; docsRoute?: unknown }
      const available = o.available === true
      const reason =
        typeof o.reason === 'string' && o.reason.trim() !== ''
          ? o.reason
          : available
            ? null
            : SYNTHESIZED_REASON
      out[k] = {
        available,
        reason,
        docsRoute: typeof o.docsRoute === 'string' ? o.docsRoute : undefined,
      }
    }
  }
  return out
}

// ---------- provider ----------

interface CapabilitiesStore {
  scopes: Record<string, Record<string, Capability>>
  report: (scope: CapabilityScope, raw: unknown) => void
  get: (key: string, scope: CapabilityScope) => Capability
}

const CapabilitiesContext = createContext<CapabilitiesStore | null>(null)

export function CapabilitiesProvider({ children }: { children: ReactNode }) {
  const [scopes, setScopes] = useState<Record<string, Record<string, Capability>>>({})

  // 同一 scope 的 snapshot 为 replace 语义（仅当该 meta 携带 capabilities 字段时）；
  // 不跨 scope merge——离开 scope 即失效
  const report = useCallback((scope: CapabilityScope, raw: unknown) => {
    const parsed = parseServerCapabilities(raw)
    if (Object.keys(parsed).length === 0) return
    const key = scopeKey(scope)
    setScopes((prev) => ({ ...prev, [key]: parsed }))
  }, [])

  const get = useCallback(
    (key: string, scope: CapabilityScope): Capability =>
      scopes[scopeKey(scope)]?.[key] ??
      (staticRegistry as Record<string, Capability>)[key] ??
      FALLBACK_CLOSED,
    [scopes],
  )

  const value = useMemo<CapabilitiesStore>(() => ({ scopes, report, get }), [scopes, report, get])
  return <CapabilitiesContext.Provider value={value}>{children}</CapabilitiesContext.Provider>
}

/** React 读取入口：当前 scope 的 server 值 → 静态 fail-closed → 未声明 fail-closed */
export function useCapability(key: CapabilityKey, scope: CapabilityScope = GLOBAL_SCOPE): Capability {
  const ctx = useContext(CapabilitiesContext)
  // provider 外（如孤立组件单测）回退静态 fail-closed
  if (!ctx) return capability(key)
  return ctx.get(key, scope)
}

/** API hooks 在拿到 meta 时调用：把 meta.capabilities（权威值）按所属 scope 推入 store */
export function useReportCapabilities(
  meta: ResponseMeta | null | undefined,
  scope: CapabilityScope = GLOBAL_SCOPE,
): void {
  const ctx = useContext(CapabilitiesContext)
  const report = ctx?.report
  const key = scopeKey(scope)
  useEffect(() => {
    if (report && meta?.capabilities) report(scope, meta.capabilities)
    // scope key 变化即换 snapshot 目标；report 引用稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report, key, meta])
}
