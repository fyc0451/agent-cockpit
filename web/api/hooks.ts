import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from './client'
import {
  GLOBAL_SCOPE,
  projectScope,
  useReportCapabilities,
  workspaceScope,
  type CapabilityScope,
} from '../state/capabilities'
import type { ApiResult } from './client'
import type {
  Attention,
  EnvCheck,
  HerdrStatus,
  Overview,
  Project,
  Settings,
  Tasks,
  Workbench,
} from './types'

function shouldRetry(failureCount: number, error: unknown): boolean {
  return error instanceof ApiError && error.retryable && failureCount < 2
}

const retry = { retry: shouldRetry }

/** meta.capabilities 是能力权威值：每个 hook 拿到 meta 后按所属 scope（从 query key 取）上报 */
function useMeta<T>(q: { data?: ApiResult<T> }, scope: CapabilityScope = GLOBAL_SCOPE) {
  useReportCapabilities(q.data?.meta, scope)
}

export function useOverview() {
  const q = useQuery({
    queryKey: ['overview'],
    queryFn: () => api.get<Overview>('/api/overview'),
    staleTime: 15_000,
    ...retry,
  })
  useMeta(q)
  return q
}

export function useAttention() {
  const q = useQuery({
    queryKey: ['attention'],
    queryFn: () => api.get<Attention>('/api/attention'),
    staleTime: 10_000,
    ...retry,
  })
  useMeta(q)
  return q
}

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

export function useSettings() {
  const q = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<Settings>('/api/settings'),
    staleTime: 30_000,
    ...retry,
  })
  useMeta(q)
  return q
}

export function useEnvCheck() {
  const q = useQuery({
    queryKey: ['env-check'],
    queryFn: () => api.get<EnvCheck>('/api/env-check'),
    staleTime: 60_000,
    ...retry,
  })
  useMeta(q)
  return q
}

export function useHerdrStatus() {
  const q = useQuery({
    queryKey: ['herdr-status'],
    queryFn: () => api.get<HerdrStatus>('/api/herdr/status'),
    staleTime: 15_000,
    refetchInterval: 30_000,
    ...retry,
  })
  useMeta(q)
  return q
}

export function useTasks(filter: { project?: string; workspace?: string }) {
  const params = new URLSearchParams()
  if (filter.project) params.set('project', filter.project)
  if (filter.workspace) params.set('workspace', filter.workspace)
  const qs = params.toString()
  const q = useQuery({
    queryKey: ['tasks', filter.project ?? null, filter.workspace ?? null],
    queryFn: () => api.get<Tasks>(`/api/tasks${qs ? `?${qs}` : ''}`),
    staleTime: 10_000,
    ...retry,
  })
  useMeta(
    q,
    filter.project && filter.workspace
      ? workspaceScope(filter.project, filter.workspace)
      : filter.project
        ? projectScope(filter.project)
        : GLOBAL_SCOPE,
  )
  return q
}
