import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  attachWorkspacePreparation,
  createWorkspaceMember,
  createWorkspacePreparation,
  detachWorkspacePreparation,
  getWorkspacePreparation,
  listWorkspaceMembers,
  type WorkspacePreparation,
} from '../../api/workspaceExecution'
import { newIdempotencyKey } from '../../api/idempotency'
import { Button } from '../../components/Button'

function messageForError(error: unknown): string {
  if (!(error instanceof ApiError)) return '暂时无法完成执行准备。请重试。'
  if (error.code === 'disconnected') return '当前无法连接服务。已保留刚才的准备状态。'
  if (error.code === 'source_not_git') return '当前源工作区不是 Git 项目，无法准备独立现场。'
  if (error.code === 'source_dirty') return '工作区有未提交更改，无法准备或连接。当前状态已保留。'
  if (error.code === 'checkout_conflict') return '独立现场与当前成员冲突。当前状态已保留。'
  if (error.code === 'lease_conflict') return '执行预留冲突。当前状态已保留。'
  if (error.code === 'stale_revision') return '准备状态已变化，请刷新后重试。'
  if (error.code === 'runtime_unavailable') return '只读 Agent 暂时不可用。当前状态已保留。'
  if (error.code === 'runtime_identity_unverified') return '无法核验成员身份。当前状态已保留。'
  if (error.code === 'process_exited') return '只读连接已退出。当前状态已保留。'
  if (error.code === 'idempotency_conflict') return '请求冲突。当前状态已保留。'
  if (error.status === 409) return '准备未完成。当前状态已保留。'
  return '暂时无法完成执行准备。当前状态已保留。'
}

function canAttach(state: string | undefined): boolean {
  return state === 'prepared' || state === 'detached' || state === 'outcome_unknown'
}

function isScopeNotFound(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 404) return false
  return (
    error.code === 'project_not_found' ||
    error.code === 'workspace_not_found' ||
    error.code === 'work_item_not_found'
  )
}

function useIntentKey(binding: string): [string, () => void] {
  const [state, setState] = useState(() => ({ binding, key: newIdempotencyKey() }))
  if (state.binding !== binding) {
    setState({ binding, key: newIdempotencyKey() })
  }
  return [state.key, () => setState({ binding, key: newIdempotencyKey() })]
}

export function WorkPreparation({
  projectId,
  workspaceId,
  workItemId,
}: {
  projectId: string
  workspaceId: string
  workItemId: string
}) {
  const queryClient = useQueryClient()
  const membersKey = ['workspace-execution-members', projectId, workspaceId] as const
  const prepKey = ['workspace-execution-prep', projectId, workspaceId, workItemId] as const
  const membersQuery = useQuery({
    queryKey: membersKey,
    queryFn: () => listWorkspaceMembers(projectId, workspaceId),
    retry: (count, error) => error instanceof ApiError && error.retryable && count < 2,
  })
  const prepQuery = useQuery({
    queryKey: prepKey,
    queryFn: () => getWorkspacePreparation(projectId, workspaceId, workItemId),
    retry: (count, error) => error instanceof ApiError && error.retryable && count < 2,
  })

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [displayName, setDisplayName] = useState('')
  const [nameKey, setNameKey] = useState(newIdempotencyKey)
  const [actionError, setActionError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const members = membersQuery.data?.data.items ?? []
  const prep = prepQuery.data?.data ?? null
  const scopeMissing = isScopeNotFound(membersQuery.error) || isScopeNotFound(prepQuery.error)
  const [prepareKey, rotatePrepareKey] = useIntentKey(selectedId ?? '')
  const [attachKey, rotateAttachKey] = useIntentKey(prep ? `a:${prep.revision}` : 'a')
  const [detachKey, rotateDetachKey] = useIntentKey(prep ? `d:${prep.revision}` : 'd')

  useEffect(() => {
    if (prep?.identity.identity_id) setSelectedId(prep.identity.identity_id)
  }, [prep?.identity.identity_id])

  const changeName = (value: string) => {
    setDisplayName(value)
    setNameKey(newIdempotencyKey())
    setActionError(null)
  }

  const run = async (action: () => Promise<WorkspacePreparation | void>) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setActionError(null)
    try {
      const next = await action()
      if (next) queryClient.setQueryData(prepKey, { data: next, meta: null })
      await queryClient.invalidateQueries({ queryKey: membersKey })
    } catch (error) {
      setActionError(error)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const createMember = (event: FormEvent) => {
    event.preventDefault()
    const name = displayName.trim()
    if (name === '' || name.length > 64) return
    void run(async () => {
      const created = await createWorkspaceMember(projectId, workspaceId, name, nameKey)
      setSelectedId(created.data.identity_id)
      setDisplayName('')
      setNameKey(newIdempotencyKey())
    })
  }

  const prepare = () => {
    if (!selectedId) return
    void run(async () => {
      const result = await createWorkspacePreparation(
        projectId, workspaceId, workItemId, selectedId, prepareKey,
      )
      rotatePrepareKey()
      return result.data
    })
  }

  const attach = () => {
    if (!prep) return
    void run(async () => {
      const result = await attachWorkspacePreparation(
        projectId, workspaceId, workItemId, prep.revision, attachKey,
      )
      rotateAttachKey()
      return result.data
    })
  }

  const detach = () => {
    if (!prep) return
    void run(async () => {
      const result = await detachWorkspacePreparation(
        projectId, workspaceId, workItemId, prep.revision, detachKey,
      )
      rotateDetachKey()
      return result.data
    })
  }

  const state = prep?.state
  const selectedMember = members.find((item) => item.identity_id === selectedId) ?? prep?.identity ?? null

  return (
    <section className="work-prep" aria-label="执行准备">
      <h3 className="work-prep-title">执行准备</h3>
      <p className="work-prep-status">
        <span>未分配</span>
        <span>尚未领取</span>
      </p>
      {prepQuery.isError || membersQuery.isError ? (
        <p className="focus-inline-error" role="alert">无法完整读取执行准备。已保留当前已知状态。</p>
      ) : null}

      {scopeMissing ? null : !prep ? (
        <>
          <ul className="work-prep-members">
            {members.map((item) => (
              <li key={item.identity_id}>
                <label className="work-prep-member">
                  <input
                    type="radio"
                    name="work-prep-member"
                    checked={selectedId === item.identity_id}
                    disabled={busy}
                    onChange={() => {
                      setSelectedId(item.identity_id)
                      setActionError(null)
                    }}
                  />
                  <span>{item.display_name}</span>
                </label>
              </li>
            ))}
          </ul>
          <form className="work-prep-create" onSubmit={createMember}>
            <label htmlFor="work-prep-name">成员名称</label>
            <input
              id="work-prep-name"
              value={displayName}
              maxLength={64}
              disabled={busy}
              onChange={(event) => changeName(event.target.value)}
            />
            <Button type="submit" disabled={busy || displayName.trim() === ''}>新建成员</Button>
          </form>
          <Button variant="primary" type="button" disabled={busy || !selectedId} onClick={prepare}>
            准备执行
          </Button>
        </>
      ) : (
        <>
          <p className="work-prep-member-name">{selectedMember?.display_name ?? prep.identity.display_name}</p>
          {state === 'connected_readonly' ? (
            <p className="work-prep-phase">已连接（只读，尚未领取）</p>
          ) : (
            <p className="work-prep-phase">已准备（独立 Checkout，尚未领取）</p>
          )}
          {state === 'connected_readonly' ? (
            <Button variant="primary" type="button" disabled={busy} onClick={detach}>断开</Button>
          ) : (
            <Button
              variant="primary"
              type="button"
              disabled={busy || (!canAttach(state) && state !== 'prepared')}
              onClick={attach}
            >
              连接只读 Agent
            </Button>
          )}
          {prep.checkout ? (
            <details className="work-prep-evidence">
              <summary>技术摘要</summary>
              <p>source {prep.checkout.source_head.slice(0, 12)} / {prep.checkout.source_tree.slice(0, 12)}</p>
              {prep.attachment ? (
                <p>{prep.attachment.provider} · {prep.attachment.harness}</p>
              ) : null}
            </details>
          ) : null}
        </>
      )}
      {actionError ? <p className="focus-inline-error" role="alert">{messageForError(actionError)}</p> : null}
    </section>
  )
}
