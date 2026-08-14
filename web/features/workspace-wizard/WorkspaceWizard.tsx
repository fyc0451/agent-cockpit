// P0-WORKSPACE-001-F：创建 Workspace 向导（单步表单；shared-only）。
// 合同：/tmp/p0-workspace001-claude/REPORT.md r2 §3。纪律（对齐 ProjectWizard 幂等惯例）：
// - 提交必须带 Idempotency-Key 且与逐字节序列化 body 绑定：重试复用同 key，任何字段
//   变化生成新 UUID（I2/T11）。
// - 取消/Escape/overlay 关闭零请求；禁用提交零请求；不 toast 假成功。
// - 409/404/503 原地 typed 表达；retryable 才给重试；envelope/DTO 守卫失败 → protocol_error。
// - 成功：invalidate ['local-ws-list', projectId] 并深链 Workspace Files（成功反馈=落地页，
//   无成功卡片/Toast）。名称/目标按原样字符串提交（含首尾空白），仅按合同长度校验。

import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { newIdempotencyKey } from '../../api/idempotency'
import { useCreateWorkspace, type CreateWorkspaceRequest } from '../../api/workspaceCreate'
import type { RepoLocationSummary } from '../../api/registry'
import { routes } from '../../app/routes'
import { Button } from '../../components/Button'
import { StatusState } from '../../components/StatusState'
import { useDialog } from '../../components/useDialog'

const NAME_MAX = 256
const GOAL_MAX = 4096

/**
 * 选中 RepoLocation 解析：未显式选择 → 默认第一项；显式选择项掉出合格集
 * （打开期间列表刷新）→ null，提交 fail-closed，绝不静默改选第一项。
 */
export function resolveSelectedRepo(
  repos: RepoLocationSummary[],
  repoId: string | null,
): RepoLocationSummary | null {
  if (repoId == null) return repos[0] ?? null
  return repos.find((r) => r.repo_location_id === repoId) ?? null
}

export function WorkspaceWizard({
  open,
  onClose,
  projectSlug,
  projectId,
  repos,
}: {
  open: boolean
  onClose: () => void
  projectSlug: string
  projectId: string
  /** 仅合格项（active+local+available），由 gateWorkspaceCreate 提供 */
  repos: RepoLocationSummary[]
}) {
  const ref = useRef<HTMLElement>(null)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const create = useCreateWorkspace()

  const [repoId, setRepoId] = useState<string | null>(null)
  const [name, setName] = useState('main')
  const [goal, setGoal] = useState('')
  const [submitBinding, setSubmitBinding] = useState<{
    serializedBody: string
    idempotencyKey: string
  } | null>(null)
  const [submitError, setSubmitError] = useState<ApiError | null>(null)

  useDialog(ref, open, onClose)

  // 重开完整重置（不残留 Idempotency-Key/错误）
  useEffect(() => {
    if (open) {
      setRepoId(null)
      setName('main')
      setGoal('')
      setSubmitBinding(null)
      setSubmitError(null)
    }
  }, [open])

  if (!open) return null

  // 打开期间列表可能刷新：显式所选 repo 掉出合格集 → fail-closed 禁提交（不静默改选）
  const selected = resolveSelectedRepo(repos, repoId)

  // 名称/目标按原样字符串提交（含首尾空白）：仅按合同长度校验，不做 trim
  const nameOk = name.length >= 1 && name.length <= NAME_MAX
  const goalOk = goal.length <= GOAL_MAX
  const submitReason = !selected
    ? '所选项目目录已不可用'
    : !nameOk
      ? name.length === 0
        ? '请填写工作空间名称'
        : `名称最长 ${NAME_MAX} 字符`
      : !goalOk
        ? `目标最长 ${GOAL_MAX} 字符`
        : create.isPending
          ? '创建在途，请稍候'
          : null

  const submit = () => {
    if (!selected || submitReason != null) return
    const req: CreateWorkspaceRequest = {
      repo_location_id: selected.repo_location_id,
      name,
      goal: goal === '' ? null : goal,
      isolation_kind: 'shared',
    }
    // 幂等绑定：body 逐字节相同 → 复用旧 key（同 key 同 body 重试）；否则新 UUID
    const serializedBody = JSON.stringify(req)
    const binding =
      submitBinding && submitBinding.serializedBody === serializedBody
        ? submitBinding
        : { serializedBody, idempotencyKey: newIdempotencyKey() }
    setSubmitBinding(binding)
    setSubmitError(null)
    create.mutate(
      { projectId, req, idempotencyKey: binding.idempotencyKey },
      {
        onSuccess: (res) => {
          void queryClient.invalidateQueries({ queryKey: ['local-ws-list', projectId] })
          onClose()
          navigate(routes.workspace.agent(projectSlug, res.data.workspace_id))
        },
        onError: (err) => setSubmitError(err as ApiError),
      },
    )
  }

  return (
    <div className="overlay" onClick={onClose}>
      <section
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="创建工作空间"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2 className="panel-title">创建工作空间</h2>
          <button type="button" className="btn btn--icon" aria-label="关闭" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="kv-grid">
          {repos.length > 1 ? (
            <>
              <label className="kv-key" htmlFor="ws-create-repo">
                项目目录
              </label>
              <select
                id="ws-create-repo"
                className="input"
                aria-label="项目目录"
                value={selected?.repo_location_id ?? ''}
                onChange={(e) => setRepoId(e.target.value)}
              >
                {repos.map((r, index) => (
                  <option key={r.repo_location_id} value={r.repo_location_id}>
                    项目目录 {index + 1}（{r.vcs_kind === 'git' ? 'Git' : '普通目录'}）
                  </option>
                ))}
              </select>
            </>
          ) : null}
          <label className="kv-key" htmlFor="ws-create-name">
            工作空间名称
          </label>
          <input
            id="ws-create-name"
            className="input"
            aria-label="工作空间名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <label className="kv-key" htmlFor="ws-create-goal">
            工作空间说明（可选）
          </label>
          <input
            id="ws-create-goal"
            className="input"
            aria-label="工作空间说明（可选）"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
        </div>

        {name.length > NAME_MAX ? (
          <p className="state-reason" role="alert">
            名称最长 {NAME_MAX} 字符
          </p>
        ) : null}

        {submitError ? <SubmitErrorNote error={submitError} onRetry={submit} /> : null}

        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button
            variant="primary"
            disabled={submitReason != null}
            title={submitReason ?? undefined}
            onClick={submit}
          >
            {create.isPending ? '创建中…' : '创建并打开'}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
        </div>
      </section>
    </div>
  )
}

function SubmitErrorNote({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  if (
    error.code === 'repo_location_not_found' ||
    error.code === 'repo_location_not_local' ||
    error.code === 'repo_location_unavailable'
  ) {
    return (
      <StatusState
        kind="conflict"
        banner
        title="项目目录不可用"
        description="项目目录状态已变化，请返回项目后重试。"
      />
    )
  }
  if (error.status === 409) {
    if (error.code === 'workspace_name_conflict') {
      return (
        <StatusState
          kind="conflict"
          banner
          title="同名工作空间已存在"
          description="请换一个名称后再创建。"
        />
      )
    }
    if (error.code === 'idempotency_conflict') {
      return (
        <StatusState
          kind="error"
          banner
          title="提交状态冲突"
          description="请关闭窗口后重新创建。"
        />
      )
    }
    // repo_location_not_local / repo_location_unavailable 等 409：数据已变化， typed 表达
    return <StatusState kind="conflict" banner description={error.message} />
  }
  // retryable（503/网络）→ 重试复用同一 Idempotency-Key 与 body
  return (
    <>
      <StatusState
        kind={error.retryable ? 'disconnected' : 'error'}
        banner
        description={error.message}
      />
      {error.retryable ? (
        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button variant="primary" onClick={onRetry}>
            重试
          </Button>
        </div>
      ) : null}
    </>
  )
}
