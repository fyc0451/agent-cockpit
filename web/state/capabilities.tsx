// Capability 权威合并层（本车核心红线）：
// - 静态 registry 是 fail-closed fallback（全部 available=false + 真实 reason）；
// - 任何 query 返回的 meta.capabilities 是权威值，经 useReportCapabilities 推入
//   CapabilitiesProvider store；读取顺序：server 值 → 静态 fallback → 未声明 fail-closed。
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
    reason: 'Memory/Context Pack 规划在 W4 接通',
    docsRoute: DOCTOR,
  },
  'recovery.review': {
    available: false,
    reason: '恢复审核 API 未接通（W1）',
    docsRoute: DOCTOR,
  },
  'activity.feed': {
    available: false,
    reason: '项目/Workspace 活动流 API 未接通（W1）',
    docsRoute: DOCTOR,
  },
  'git.integration': {
    available: false,
    reason: 'Git 集成 API 未接通（W1）',
    docsRoute: DOCTOR,
  },
  'editor.embedded': {
    available: false,
    reason: '内嵌编辑器规划在后续迭代接通',
    docsRoute: DOCTOR,
  },
  browser: {
    available: false,
    reason: '嵌入式浏览器规划在后续迭代接通',
    docsRoute: DOCTOR,
  },
  automation: {
    available: false,
    reason: '自动化 API 未接通（W1）',
    docsRoute: DOCTOR,
  },
  'terminal.pty': {
    available: false,
    reason: 'PTY 未接通：W1 仅终端外壳，不写任何假输出',
    docsRoute: DOCTOR,
  },
  'search.server': {
    available: false,
    reason: '服务端搜索未接通，仅支持页面导航',
    docsRoute: DOCTOR,
  },
  remoteHerdr: {
    available: false,
    reason: '远程 Herdr 控制未接通（W1）',
    docsRoute: DOCTOR,
  },
  harnessCatalog: {
    available: false,
    reason: 'Harness 目录 API 未接通',
    docsRoute: DOCTOR,
  },
  'projectRegistry.write': {
    available: false,
    reason: '项目注册写操作未开放（W1 只读）',
    docsRoute: DOCTOR,
  },
  'files.read': {
    available: false,
    reason: 'Workspace 文件 facade API 未接通（后端 Workspace 文件门面未就绪，W1 禁止回退全局 legacy /api/files/*）',
    docsRoute: DOCTOR,
  },
  'workspace.delete': {
    available: false,
    reason: 'Workspace 删除未开放（W1 只读骨架）',
    docsRoute: DOCTOR,
  },
  'settings.write': {
    available: false,
    reason: '设置写操作未开放（W1 只读）',
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
  reason: '能力未声明，默认关闭（fail-closed）',
  docsRoute: DOCTOR,
}

/** 宽容解析 meta.capabilities：支持 boolean 与 { available, reason?, docsRoute? } 两种形态 */
export function parseServerCapabilities(raw: unknown): Record<string, Capability> {
  if (!raw || typeof raw !== 'object') return {}
  const out: Record<string, Capability> = {}
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'boolean') {
      out[k] = { available: v, reason: v ? null : '服务端标记该能力不可用' }
    } else if (v && typeof v === 'object') {
      const o = v as { available?: unknown; reason?: unknown; docsRoute?: unknown }
      out[k] = {
        available: o.available === true,
        reason: typeof o.reason === 'string' ? o.reason : null,
        docsRoute: typeof o.docsRoute === 'string' ? o.docsRoute : undefined,
      }
    }
  }
  return out
}

interface CapabilitiesStore {
  server: Record<string, Capability>
  merge: (raw: unknown) => void
  get: (key: string) => Capability
}

const CapabilitiesContext = createContext<CapabilitiesStore | null>(null)

export function CapabilitiesProvider({ children }: { children: ReactNode }) {
  const [server, setServer] = useState<Record<string, Capability>>({})

  const merge = useCallback((raw: unknown) => {
    const parsed = parseServerCapabilities(raw)
    if (Object.keys(parsed).length === 0) return
    setServer((prev) => ({ ...prev, ...parsed }))
  }, [])

  const get = useCallback(
    (key: string): Capability => server[key] ?? (staticRegistry as Record<string, Capability>)[key] ?? FALLBACK_CLOSED,
    [server],
  )

  const value = useMemo<CapabilitiesStore>(() => ({ server, merge, get }), [server, merge, get])
  return <CapabilitiesContext.Provider value={value}>{children}</CapabilitiesContext.Provider>
}

/** React 读取入口：server 权威值 → 静态 fail-closed → 未声明 fail-closed */
export function useCapability(key: CapabilityKey): Capability {
  const ctx = useContext(CapabilitiesContext)
  // provider 外（如孤立组件单测）回退静态 fail-closed
  if (!ctx) return capability(key)
  return ctx.get(key)
}

/** API hooks 在拿到 meta 时调用：把 meta.capabilities（权威值）推入 provider store */
export function useReportCapabilities(meta: ResponseMeta | null | undefined): void {
  const ctx = useContext(CapabilitiesContext)
  const merge = ctx?.merge
  useEffect(() => {
    if (merge && meta?.capabilities) merge(meta.capabilities)
  }, [merge, meta])
}
