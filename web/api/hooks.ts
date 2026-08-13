import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from './client'
import {
  GLOBAL_SCOPE,
  projectScope,
  useReportCapabilities,
  type CapabilityScope,
} from '../state/capabilities'
import type { ApiResult } from './client'
import type { Project, Workbench } from './types'
import {
  legacyGet,
  assertLegacyOverview,
  assertLegacyAttention,
  assertLegacySettings,
  assertLegacyHerdrStatus,
  assertLegacyEnvCheck,
  assertLegacyTasks,
} from './localSlice'

function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.retryable && failureCount < 2
}

const retry = { retry: shouldRetry }

/** meta.capabilities 是能力权威值：每个 hook 拿到 meta 后按所属 scope（从 query key 取）上报 */
function useMeta<T>(q: { data?: ApiResult<T> }, scope: CapabilityScope = GLOBAL_SCOPE) {
  useReportCapabilities(q.data?.meta, scope)
}

// ---- WEB-004: legacy endpoints return bare dict, not G3 {data,meta}.
//      Use legacyGet + shape guard; return {data, meta:null} (no fabricated meta).
//      Do NOT call useMeta (legacy has no real capabilities to report).

export function useOverview() {
  const q = useQuery({
    queryKey: ['overview'],
    queryFn: async () => ({ data: assertLegacyOverview(await legacyGet('/api/overview')), meta: null }),
    staleTime: 15_000,
    ...retry,
  })
  return q
}

export function useAttention() {
  const q = useQuery({
    queryKey: ['attention'],
    queryFn: async () => ({ data: assertLegacyAttention(await legacyGet('/api/attention')), meta: null }),
    staleTime: 10_000,
    ...retry,
  })
  return q
}

// ---- Unchanged: useProject/useWorkbench not consumed by production pages ----

export function useProject(slug: string | null) {
  const q = useQuery({
    queryKey: ['project', slug],
    queryFn: () => api.get<Project>(`/api/projects/${encodeURIComponent(slug!)}`),
    enabled: slug != null,
    staleTime: 30_000,
    ...retry,
  })
  useMeta(q, slug ? projectScope(slug) : GLOBAL_SCOPE)
  return q
}

export function useWorkbench(slug: string | null) {
  const q = useQuery({
    queryKey: ['workbench', slug],
    queryFn: () => api.get<Workbench>(`/api/projects/${encodeURIComponent(slug!)}/workbench`),
    enabled: slug != null,
    staleTime: 15_000,
    ...retry,
  })
  useMeta(q, slug ? projectScope(slug) : GLOBAL_SCOPE)
  return q
}

// ---- WEB-004 legacy adapters (continued) ----

export function useSettings() {
  const q = useQuery({
    queryKey: ['settings'],
    queryFn: async () => ({ data: assertLegacySettings(await legacyGet('/api/settings')), meta: null }),
    staleTime: 30_000,
    ...retry,
  })
  return q
}

export function useEnvCheck() {
  const q = useQuery({
    queryKey: ['env-check'],
    queryFn: async () => ({ data: assertLegacyEnvCheck(await legacyGet('/api/env-check')), meta: null }),
    staleTime: 60_000,
    ...retry,
  })
  return q
}

export function useHerdrStatus() {
  const q = useQuery({
    queryKey: ['herdr-status'],
    queryFn: async () => ({ data: assertLegacyHerdrStatus(await legacyGet('/api/herdr/status')), meta: null }),
    staleTime: 15_000,
    refetchInterval: 30_000,
    ...retry,
  })
  return q
}

export function useTasks(filter: { project?: string; workspace?: string }) {
  const params = new URLSearchParams()
  if (filter.project) params.set('project', filter.project)
  if (filter.workspace) params.set('workspace', filter.workspace)
  const qs = params.toString()
  const q = useQuery({
    queryKey: ['tasks', filter.project ?? null, filter.workspace ?? null],
    queryFn: async () => ({ data: assertLegacyTasks(await legacyGet(`/api/tasks${qs ? `?${qs}` : ''}`)), meta: null }),
    staleTime: 10_000,
    ...retry,
  })
  return q
}
