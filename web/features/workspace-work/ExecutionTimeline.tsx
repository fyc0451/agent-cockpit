import { useQuery } from '@tanstack/react-query'
import { ApiError } from '../../api/client'
import {
  getWorkspaceWorkDetail,
  type WorkspaceWorkDetail,
} from '../../api/workspaceWorkDetail'
import { StatusState } from '../../components/StatusState'

/**
 * D-W1 执行时间线：状态只由 GET detail 的 thread/messages/work_item/
 * claim/receipts 推导，绝不伪造 working，不显示 transcript/Pane/摘要之外的
 * 任何技术证据。等待领取/工作中轮询，终态停轮询。
 */

export type ExecutionPhase =
  | 'dispatching'      // 派遣意图已持久化，等待唤醒结果
  | 'delivered'      // 已派遣，等待领取（unassigned + delivery 成功回执）
  | 'delivery_unknown' // 派遣结果未知（unassigned + delivery outcome_unknown）
  | 'working'        // 工作中（work_item.status='working'）
  | 'failed'         // typed failure（status='failed' 或 failure 回执）
  | 'completed'      // 已完成（status='completed'）

export interface ExecutionTimelineModel {
  phase: ExecutionPhase
  failureReason: string | null
  agentReplies: WorkspaceWorkDetail['thread']['messages']
  timeline: TimelineEntry[]
}

export interface TimelineEntry {
  key: string
  label: string
  detail: string | null
  tone: 'ok' | 'pending' | 'failure'
}

export function deriveExecutionTimeline(
  detail: WorkspaceWorkDetail,
): ExecutionTimelineModel {
  const { work_item: work, receipts } = detail
  const agentReplies = detail.thread.messages.filter(
    (message) => message.message_kind === 'reply' && message.author_kind === 'agent',
  )
  const failureReceipt = receipts.find((receipt) => (
    receipt.kind === 'failure' && receipt.outcome === 'failed'
  ))
  const lastDelivery = [...receipts]
    .reverse()
    .find((receipt) => (
      receipt.kind === 'delivery'
      && (
        receipt.outcome === 'intent'
        || receipt.outcome === 'succeeded'
        || receipt.outcome === 'outcome_unknown'
      )
    ))

  let phase: ExecutionPhase
  if (work.status === 'completed') {
    phase = 'completed'
  } else if (failureReceipt) {
    phase = 'failed'
  } else if (work.status === 'working') {
    phase = 'working'
  } else if (lastDelivery?.outcome === 'outcome_unknown') {
    phase = 'delivery_unknown'
  } else if (lastDelivery?.outcome === 'succeeded') {
    phase = 'delivered'
  } else {
    phase = 'dispatching'
  }

  const timeline: TimelineEntry[] = []
  if (lastDelivery) {
    if (lastDelivery.outcome === 'outcome_unknown') {
      timeline.push({
        key: lastDelivery.receipt_id,
        label: '派遣结果未知',
        detail: '唤醒结果未确认，可重试派遣',
        tone: 'failure',
      })
    } else if (lastDelivery.outcome === 'intent') {
      timeline.push({
        key: lastDelivery.receipt_id,
        label: '派遣已记录',
        detail: '等待唤醒确认',
        tone: 'pending',
      })
    } else {
      timeline.push({
        key: lastDelivery.receipt_id,
        label: '已派遣',
        detail: '等待成员领取',
        tone: 'pending',
      })
    }
  }
  const claimReceipt = receipts.find((receipt) => (
    receipt.kind === 'claim'
    && receipt.outcome === 'ok'
    && detail.claim !== null
    && receipt.claim_id === detail.claim.claim_id
  ))
  if (claimReceipt) {
    timeline.push({
      key: claimReceipt.receipt_id,
      label: '已领取',
      detail: null,
      tone: 'ok',
    })
  }
  const replyReceipt = receipts.find((receipt) => (
    receipt.kind === 'reply'
    && receipt.outcome === 'ok'
    && agentReplies.some((message) => message.message_id === receipt.message_id)
  ))
  if (replyReceipt) {
    timeline.push({
      key: replyReceipt.receipt_id,
      label: '已回复',
      detail: null,
      tone: 'ok',
    })
  }
  const completeReceipt = receipts.find((receipt) => (
    receipt.kind === 'complete'
    && receipt.outcome === 'ok'
    && agentReplies.some((message) => message.message_id === receipt.message_id)
  ))
  if (work.status === 'completed' && completeReceipt) {
    timeline.push({
      key: completeReceipt.receipt_id,
      label: '已完成',
      detail: null,
      tone: 'ok',
    })
  }
  if (failureReceipt) {
    timeline.push({
      key: failureReceipt.receipt_id,
      label: '失败',
      detail: failureReceipt.reason,
      tone: 'failure',
    })
  }

  return {
    phase,
    failureReason: failureReceipt?.reason ?? null,
    agentReplies,
    timeline,
  }
}

export function hasExecutionStarted(detail: WorkspaceWorkDetail): boolean {
  return detail.work_item.status !== 'unassigned' || detail.receipts.some(
    (receipt) => (
      receipt.kind === 'failure' && receipt.outcome === 'failed'
    ) || (
      receipt.kind === 'delivery'
      && (
        receipt.outcome === 'intent'
        || receipt.outcome === 'succeeded'
        || receipt.outcome === 'outcome_unknown'
      )
    ),
  )
}

export function executionTimelineRefetchInterval(
  detail: WorkspaceWorkDetail | undefined,
): number | false {
  if (!detail) return false
  if (detail.work_item.status === 'working') return 5000
  if (
    detail.work_item.status === 'unassigned'
    && detail.receipts.some((receipt) => (
      receipt.kind === 'delivery'
      && (
        receipt.outcome === 'intent'
        || receipt.outcome === 'succeeded'
        || receipt.outcome === 'outcome_unknown'
      )
    ))
  ) {
    return 5000
  }
  return false
}

const PHASE_TITLES: Record<ExecutionPhase, string> = {
  dispatching: '派遣确认中',
  delivered: '已派遣，等待领取',
  delivery_unknown: '派遣结果未知',
  working: '成员工作中',
  failed: '执行失败',
  completed: '已完成',
}

export function ExecutionTimeline({
  projectId,
  workspaceId,
  workItemId,
}: {
  projectId: string
  workspaceId: string
  workItemId: string
}) {
  const queryKey = [
    'workspace-work-detail', projectId, workspaceId, workItemId,
  ] as const
  const query = useQuery({
    queryKey,
    queryFn: () => getWorkspaceWorkDetail(projectId, workspaceId, workItemId),
    retry: (failureCount, error) =>
      error instanceof ApiError && error.retryable && failureCount < 2,
    refetchInterval: (query) => {
      const detail = query.state.data?.data
      return executionTimelineRefetchInterval(detail)
    },
  })

  if (query.isPending) {
    return (
      <section className="execution-timeline" aria-label="执行时间线">
        <StatusState kind="loading" title="正在加载执行时间线…" />
      </section>
    )
  }

  if (query.isError) {
    // 列表存在但 detail 404：按未开始处理（列表-详情竞态/未接线后端），
    // 不渲染执行状态；其余错误诚实降态可重试。
    if (
      query.error instanceof ApiError
      && query.error.status === 404
    ) {
      return null
    }
    return (
      <section className="execution-timeline" aria-label="执行时间线">
        <StatusState
          kind="degraded"
          title="执行时间线暂不可用"
          description="当前任务的执行状态未能读取。"
          action={{ label: '重试', onClick: () => void query.refetch() }}
        />
      </section>
    )
  }

  const detail = query.data.data
  if (!hasExecutionStarted(detail)) return null

  const model = deriveExecutionTimeline(detail)
  return (
    <section className="execution-timeline" aria-label="执行时间线">
      <h3 className="execution-timeline-title">执行时间线</h3>
      <p
        className={`execution-timeline-phase execution-timeline-phase--${model.phase}`}
        role="status"
      >
        {PHASE_TITLES[model.phase]}
      </p>
      <ol className="execution-timeline-entries">
        {model.timeline.map((entry) => (
          <li
            key={entry.key}
            className={`execution-timeline-entry execution-timeline-entry--${entry.tone}`}
          >
            <span className="execution-timeline-entry-label">{entry.label}</span>
            {entry.detail ? (
              <span className="execution-timeline-entry-detail">{entry.detail}</span>
            ) : null}
          </li>
        ))}
      </ol>
      {model.agentReplies.length > 0 ? (
        <div className="execution-timeline-replies">
          {model.agentReplies.map((reply) => (
            <article key={reply.message_id} className="execution-timeline-reply">
              <p className="focus-message-author">成员</p>
              <p className="focus-message-body">{reply.body}</p>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  )
}
