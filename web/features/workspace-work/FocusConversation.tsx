import { useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  createWorkspaceWork,
  listWorkspaceWork,
  type CreateWorkspaceWorkRequest,
  type WorkspaceWorkAggregate,
  type WorkspaceWorkListData,
} from '../../api/workspaceWork'
import type { ApiResult } from '../../api/client'
import { Button } from '../../components/Button'
import { QueryErrorState } from '../../components/QueryErrorState'
import { StatusState } from '../../components/StatusState'
import {
  clearWorkDraft,
  hasDraftContent,
  loadWorkDraft,
  updateWorkDraft,
  type WorkDraft,
  type WorkDraftField,
} from '../../state/workDraft'

const BODY_MAX = 32_768
const NOTE_MAX = 8_192

function SavedWork({ item }: { item: WorkspaceWorkAggregate }) {
  return (
    <article className="focus-message">
      <p className="focus-message-author">你</p>
      <p className="focus-message-body">{item.root_message.body}</p>
      {item.work_item.acceptance ? (
        <div className="focus-message-note">
          <h3>怎样算完成？</h3>
          <p>{item.work_item.acceptance}</p>
        </div>
      ) : null}
      {item.work_item.constraints ? (
        <div className="focus-message-note">
          <h3>需要特别注意什么？</h3>
          <p>{item.work_item.constraints}</p>
        </div>
      ) : null}
      <p className="focus-saved" role="status">工作已保存</p>
    </article>
  )
}

function messageForSaveError(error: unknown): string {
  if (!(error instanceof ApiError)) return '暂时无法保存。草稿仍保留在本机，请重试。'
  if (error.code === 'disconnected') return '当前无法连接服务。草稿仍保留在本机，请恢复连接后重试。'
  if (error.status === 409 || error.code === 'conflict') {
    return '保存意图发生冲突。草稿仍保留；修改任一字段后可作为新的工作保存。'
  }
  if (error.status === 400) return '工作内容不符合保存要求。草稿仍保留，请检查后重试。'
  return '暂时无法保存。草稿仍保留在本机，请重试。'
}

function Composer({
  projectId,
  workspaceId,
  initialDraft,
  readError,
  onSaved,
}: {
  projectId: string
  workspaceId: string
  initialDraft: WorkDraft
  readError?: unknown
  onSaved: (result: ApiResult<WorkspaceWorkAggregate>) => void
}) {
  const [draft, setDraft] = useState(initialDraft)
  const [saveError, setSaveError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  const inFlight = useRef(false)

  const change = (field: WorkDraftField, value: string) => {
    setSaveError(null)
    setDraft((current) => updateWorkDraft(projectId, workspaceId, current, field, value))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || draft.body.trim() === '' || draft.body.length > BODY_MAX) return
    inFlight.current = true
    setSaving(true)
    setSaveError(null)
    const request: CreateWorkspaceWorkRequest = {
      body: draft.body,
      acceptance: draft.acceptance.trim() === '' ? null : draft.acceptance,
      constraints: draft.constraints.trim() === '' ? null : draft.constraints,
    }
    try {
      const result = await createWorkspaceWork(projectId, workspaceId, request, draft.intentKey)
      clearWorkDraft(projectId, workspaceId)
      onSaved(result)
    } catch (error) {
      setSaveError(error)
    } finally {
      inFlight.current = false
      setSaving(false)
    }
  }

  const invalidBody = draft.body.trim() === '' || draft.body.length > BODY_MAX
  const invalidNotes = draft.acceptance.length > NOTE_MAX || draft.constraints.length > NOTE_MAX
  const unsaved = hasDraftContent(draft)

  return (
    <form className="focus-composer" onSubmit={submit}>
      {readError ? (
        <div className="focus-inline-error" role="alert">
          无法确认已保存的工作。当前本地草稿仍可重试保存。
        </div>
      ) : null}
      <label className="focus-prompt" htmlFor="focus-body">今天想推进什么？</label>
      <textarea
        id="focus-body"
        className="focus-textarea"
        value={draft.body}
        maxLength={BODY_MAX}
        readOnly={saving}
        autoFocus
        onChange={(event) => change('body', event.target.value)}
      />
      <details className="focus-details" open={draft.acceptance !== ''}>
        <summary>怎样算完成？</summary>
        <textarea
          aria-label="怎样算完成？"
          value={draft.acceptance}
          maxLength={NOTE_MAX}
          readOnly={saving}
          onChange={(event) => change('acceptance', event.target.value)}
        />
      </details>
      <details className="focus-details" open={draft.constraints !== ''}>
        <summary>需要特别注意什么？</summary>
        <textarea
          aria-label="需要特别注意什么？"
          value={draft.constraints}
          maxLength={NOTE_MAX}
          readOnly={saving}
          onChange={(event) => change('constraints', event.target.value)}
        />
      </details>
      <div className="focus-composer-footer">
        <span className="focus-draft-state">{unsaved ? '未保存' : ''}</span>
        <Button
          variant="primary"
          type="submit"
          disabled={invalidBody || invalidNotes || saving}
          title={invalidBody ? '请填写要推进的工作' : invalidNotes ? '补充说明过长' : saving ? '正在保存' : undefined}
        >
          保存工作
        </Button>
      </div>
      {saveError ? <p className="focus-inline-error" role="alert">{messageForSaveError(saveError)}</p> : null}
    </form>
  )
}

export function FocusConversation({
  projectId,
  workspaceId,
}: {
  projectId: string
  workspaceId: string
}) {
  const queryClient = useQueryClient()
  const queryKey = ['workspace-work-items', projectId, workspaceId] as const
  const [draft] = useState(() => loadWorkDraft(projectId, workspaceId))
  const query = useQuery({
    queryKey,
    queryFn: () => listWorkspaceWork(projectId, workspaceId),
    retry: (failureCount, error) => error instanceof ApiError && error.retryable && failureCount < 2,
  })

  const onSaved = (result: ApiResult<WorkspaceWorkAggregate>) => {
    queryClient.setQueryData<ApiResult<WorkspaceWorkListData>>(queryKey, (current) => ({
      data: {
        items: [...(current?.data.items ?? []), result.data],
        next_cursor: current?.data.next_cursor ?? null,
      },
      meta: result.meta,
    }))
  }

  if (query.isPending) return <StatusState kind="loading" title="正在加载工作对话…" />

  const items = query.data?.data.items ?? []
  if (query.isError && !hasDraftContent(draft)) {
    return <QueryErrorState error={query.error} onRetry={() => query.refetch()} />
  }

  return (
    <div className="focus-conversation">
      {query.data?.data.next_cursor != null || query.data?.meta?.partial === true ? (
        <StatusState kind="degraded" banner title="工作记录暂未完整加载" />
      ) : null}
      {items.map((item) => (
        <SavedWork key={item.root_message.message_id} item={item} />
      ))}
      {items.length === 0 ? (
        <Composer
          projectId={projectId}
          workspaceId={workspaceId}
          initialDraft={draft}
          readError={query.error}
          onSaved={onSaved}
        />
      ) : null}
    </div>
  )
}
