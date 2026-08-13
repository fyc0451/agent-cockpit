// P0-WORKSPACE-001-F：Workspace 创建（shared-only）的端点/类型/gating 唯一硬编码点。
// 合同：/tmp/p0-workspace001-claude/REPORT.md r2 §3（Lead Mail #3431 指定的五文件 scope 内）。
// 冻结纪律：
// - POST body 严格四键 {repo_location_id, name, goal, isolation_kind:"shared"}；
//   客户端只传 repo_location_id，绝不传绝对路径/locator/canonical_path。
// - Idempotency-Key 必须绑定逐字节序列化 body（调用方成对持有，见 WorkspaceWizard）。
// - 无 workspace.create capability 键（不臆造）；创建按钮可用性 = 数据驱动 fail-closed：
//   仅 lifecycle=active && node_id=local && availability=available 的 RepoLocation 可创建。
// - 201 响应 data 走 assertWorkspaceSummary 精确 12 键守卫（fail-closed ProtocolError）。

import { useMutation } from '@tanstack/react-query'
import { assertWorkspaceSummary } from './localSlice'
import { apiPost, type RepoLocationSummary } from './registry'

export const WORKSPACE_CREATE_API = {
  create: (projectId: string) =>
    `/api/project-registry/projects/${encodeURIComponent(projectId)}/workspaces`,
} as const

/** 严格四键；isolation_kind 本车仅 shared（其它 → 后端 400 unsupported_isolation_kind） */
export interface CreateWorkspaceRequest {
  repo_location_id: string
  name: string // 显示名，1..256；仅按长度校验，不参与路径
  goal: string | null // ≤4096 或 null
  isolation_kind: 'shared'
}

export interface WorkspaceCreateGate {
  available: boolean
  reason: string | null
  eligible: RepoLocationSummary[]
}

/**
 * 数据驱动 fail-closed gating：repo_locations 未内嵌/为空/无合格项 → 禁用 + 稳定 reason。
 * 绝不展示可能假装成功的写控件。
 */
export function gateWorkspaceCreate(
  locations: RepoLocationSummary[] | undefined,
): WorkspaceCreateGate {
  if (locations === undefined) {
    return {
      available: false,
      reason: 'RepoLocation 数据不可用，无法确认创建条件',
      eligible: [],
    }
  }
  const eligible = locations.filter(
    (l) => l.lifecycle === 'active' && l.node_id === 'local' && l.availability === 'available',
  )
  if (eligible.length > 0) return { available: true, reason: null, eligible }
  if (locations.length === 0) {
    return { available: false, reason: '该项目没有已登记的 RepoLocation', eligible: [] }
  }
  return {
    available: false,
    reason: '无满足「active · 本机 Local · 可用」条件的 RepoLocation',
    eligible: [],
  }
}

export function useCreateWorkspace() {
  return useMutation({
    mutationFn: async (vars: {
      projectId: string
      req: CreateWorkspaceRequest
      idempotencyKey: string
    }) => {
      const res = await apiPost<CreateWorkspaceRequest, unknown>(
        WORKSPACE_CREATE_API.create(vars.projectId),
        vars.req,
        { idempotencyKey: vars.idempotencyKey },
      )
      return { ...res, data: assertWorkspaceSummary(res.data, 'workspace-create') }
    },
  })
}
