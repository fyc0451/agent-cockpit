import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { matchPath, useLocation } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { PROJECT_PARAM, routePatterns, WORKSPACE_PARAM } from '../app/routes'

export interface SelectionState {
  projectSlug: string | null
  workspaceId: string | null
  /** 经 ProjectScope 校验存在后写入；URL 切换时与 workspace 原子清空 */
  project: Project | null
  workspace: Workspace | null
  setProjectScope: (project: Project | null) => void
  setWorkspaceScope: (workspace: Workspace | null) => void
}

const SelectionContext = createContext<SelectionState | null>(null)

function safeDecode(v: string | undefined): string | null {
  if (v == null) return null
  try {
    return decodeURIComponent(v)
  } catch {
    return v
  }
}

/**
 * 从当前 URL 解析 selection key：URL 形态由 app/routes.ts 的 pattern 决定。
 * matchPath 返回的 params 是编码形态（react-router v6 不解码），这里统一解码。
 */
export function parseSelection(pathname: string): {
  projectSlug: string | null
  workspaceId: string | null
} {
  const ws = matchPath(`${routePatterns.workspaceBase}/*`, pathname)
  if (ws) {
    return {
      projectSlug: safeDecode(ws.params[PROJECT_PARAM]),
      workspaceId: safeDecode(ws.params[WORKSPACE_PARAM]),
    }
  }
  const p = matchPath(`${routePatterns.projectBase}/*`, pathname)
  return { projectSlug: safeDecode(p?.params[PROJECT_PARAM]), workspaceId: null }
}

interface ScopeState {
  /** 对应 URL 的 `${projectSlug}/${workspaceId}`；key 不匹配即整包重置 */
  key: string
  project: Project | null
  workspace: Workspace | null
}

export function SelectionProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const { projectSlug, workspaceId } = parseSelection(location.pathname)
  const urlKey = `${projectSlug ?? ''}/${workspaceId ?? ''}`

  const [scope, setScope] = useState<ScopeState>({ key: urlKey, project: null, workspace: null })
  // URL 身份变化 → 同一次渲染内原子清空 project+workspace（render-phase derived state，
  // React 在提交前重渲染，不存在「新 project + 旧 workspace」的已提交中间帧）
  if (scope.key !== urlKey) {
    setScope({ key: urlKey, project: null, workspace: null })
  }

  const setProjectScope = useCallback(
    (project: Project | null) => setScope((s) => ({ ...s, project })),
    [],
  )
  const setWorkspaceScope = useCallback(
    (workspace: Workspace | null) => setScope((s) => ({ ...s, workspace })),
    [],
  )

  const value = useMemo<SelectionState>(
    () => ({
      projectSlug,
      workspaceId,
      project: scope.project,
      workspace: scope.workspace,
      setProjectScope,
      setWorkspaceScope,
    }),
    [projectSlug, workspaceId, scope.project, scope.workspace, setProjectScope, setWorkspaceScope],
  )

  return <SelectionContext.Provider value={value}>{children}</SelectionContext.Provider>
}

export function useSelection(): SelectionState {
  const ctx = useContext(SelectionContext)
  if (!ctx) throw new Error('useSelection must be used within SelectionProvider')
  return ctx
}
