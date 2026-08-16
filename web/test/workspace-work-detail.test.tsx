import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { defaultFetchMap, metaOk, REG_P1 } from '../fixtures/api'
import { renderApp, stubFetch } from './helpers'
import {
  assertWorkspaceWorkDetail,
  getWorkspaceWorkDetail,
} from '../api/workspaceWorkDetail'
import {
  deriveExecutionTimeline,
  executionTimelineRefetchInterval,
  hasExecutionStarted,
} from '../features/workspace-work/ExecutionTimeline'

/**
 * D-W1 任务时间线：GET detail exactKeys fail-closed；时间线状态只由
 * thread/messages/work_item/claim/receipts 推导（等待领取/working/typed
 * failure/reply/completed）；多任务 ?work 与刷新保持；不显示 raw
 * transcript/Pane/secret；不伪造 working。
 */

const LIST_URL = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const HOME = '/projects/p1/workspaces/w1'

const meta = metaOk

function scope(id: string) {
  return { projectId: REG_P1, workspaceId: 'w1', workItemId: id }
}

function aggregate(
  id: string,
  body: string,
  status: 'unassigned' | 'working' | 'completed' | 'failed' = 'unassigned',
) {
  return {
    thread: {
      thread_id: `thr_${id}`, project_id: REG_P1, workspace_id: 'w1',
      revision: 1, created_at: '2026-08-16T10:00:00Z',
    },
    root_message: {
      message_id: `msg_root_${id}`, thread_id: `thr_${id}`,
      author_kind: 'boss', author_ref: null, body,
    },
    work_item: {
      work_item_id: id, source_message_id: `msg_root_${id}`,
      status, acceptance: null, constraints: null,
    },
  }
}

function detail(id: string, changes: Record<string, unknown> = {}) {
  const base = {
    thread: {
      thread_id: `thr_${id}`, project_id: REG_P1, workspace_id: 'w1',
      revision: 1, created_at: '2026-08-16T10:00:00Z',
      messages: [
        {
          message_id: `msg_root_${id}`, thread_id: `thr_${id}`, ordinal: 1,
          message_kind: 'root', author_kind: 'boss', author_ref: null,
          author_generation: null, reply_to_message_id: null,
          body: `任务正文 ${id}`, created_at: '2026-08-16T10:00:00Z',
        },
      ],
    },
    work_item: {
      work_item_id: id, source_message_id: `msg_root_${id}`,
      status: 'unassigned', acceptance: null, constraints: null,
      revision: 1, updated_at: '2026-08-16T10:00:00Z',
    },
    claim: null,
    receipts: [],
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const merged = { ...base, ...changes } as any
  return { data: merged, meta }
}

function delivery(outcome: string) {
  return {
    receipt_id: `rct_d_${outcome}`, kind: 'delivery', outcome, reason: null,
    evidence_digest: 'sha256:x', created_at: '2026-08-16T10:01:00Z',
    claim_id: null, message_id: null, identity_id: null, generation: null,
  }
}

function claimReceipt() {
  return {
    receipt_id: 'rct_c_1', kind: 'claim', outcome: 'ok', reason: null,
    evidence_digest: 'sha256:x', created_at: '2026-08-16T10:02:00Z',
    claim_id: 'clm_1', message_id: null, identity_id: 'idn_1', generation: 1,
  }
}

function replyMessage(id: string) {
  return {
    message_id: `msg_reply_${id}`, thread_id: `thr_${id}`, ordinal: 2,
    message_kind: 'reply', author_kind: 'agent', author_ref: 'idn_1',
    author_generation: 1, reply_to_message_id: `msg_root_${id}`,
    body: '成员回复正文', created_at: '2026-08-16T10:05:00Z',
  }
}

function messageReceipt(id: string, kind: 'reply' | 'complete') {
  return {
    receipt_id: `rct_${kind}_${id}`, kind, outcome: 'ok', reason: null,
    evidence_digest: 'sha256:x', created_at: '2026-08-16T10:06:00Z',
    claim_id: 'clm_1', message_id: `msg_reply_${id}`,
    identity_id: 'idn_1', generation: 1,
  }
}

describe('workspaceWorkDetail fail-closed 解析', () => {
  it('合法 detail 通过且保留全部字段', () => {
    const payload = detail('wrk_a', {
      work_item: {
        work_item_id: 'wrk_a', source_message_id: 'msg_root_wrk_a',
        status: 'completed', acceptance: null, constraints: null,
        revision: 3, updated_at: '2026-08-16T10:06:00Z',
      },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_a', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [
        delivery('succeeded'), claimReceipt(),
        messageReceipt('wrk_a', 'reply'), messageReceipt('wrk_a', 'complete'),
      ],
      thread: {
        thread_id: 'thr_wrk_a', project_id: REG_P1, workspace_id: 'w1',
        revision: 2, created_at: '2026-08-16T10:00:00Z',
        messages: [
          detail('wrk_a').data.thread.messages[0],
          replyMessage('wrk_a'),
        ],
      },
    }).data
    const parsed = assertWorkspaceWorkDetail(payload, scope('wrk_a'))
    expect(parsed.work_item.status).toBe('completed')
    expect(parsed.claim?.state).toBe('closed')
    expect(parsed.receipts.map((item) => item.kind)).toEqual([
      'delivery', 'claim', 'reply', 'complete',
    ])
    expect(parsed.thread.messages.map((item) => item.message_kind)).toEqual([
      'root', 'reply',
    ])
  })

  it('未知键/缺失键/错枚举/坏序号全部 fail-closed', () => {
    const bad: unknown[] = [
      { ...detail('wrk_b').data, extra: 1 },
      { ...detail('wrk_b').data, receipts: undefined },
      {
        ...detail('wrk_b').data,
        work_item: { ...detail('wrk_b').data.work_item, status: 'working!' },
      },
      {
        ...detail('wrk_b').data,
        receipts: [{ ...delivery('succeeded'), kind: 'transcript' }],
      },
      {
        ...detail('wrk_b').data,
        receipts: [{ ...delivery('maybe') }],
      },
      {
        ...detail('wrk_b').data,
        claim: { claim_id: 'c', work_item_id: 'wrk_b', identity_id: 'i', generation: 1, state: 'active' },
      },
      {
        ...detail('wrk_b').data,
        thread: { ...detail('wrk_b').data.thread, messages: [] },
      },
    ]
    for (const payload of bad) {
      expect(() => assertWorkspaceWorkDetail(payload, scope('wrk_b'))).toThrow()
    }
  })

  it('请求 scope 与 thread/work/source/claim 关联不一致全部 fail-closed', () => {
    const base = detail('wrk_rel').data
    const bad = [
      {
        ...base,
        thread: { ...base.thread, thread_id: 'thr_other' },
      },
      {
        ...base,
        work_item: { ...base.work_item, work_item_id: 'wrk_other' },
      },
      {
        ...base,
        work_item: { ...base.work_item, source_message_id: 'msg_other' },
      },
      {
        ...base,
        claim: {
          claim_id: 'clm_1', work_item_id: 'wrk_other', identity_id: 'idn_1',
          generation: 1, state: 'pending_gate', revision: 1,
        },
      },
    ]
    for (const payload of bad) {
      expect(() => assertWorkspaceWorkDetail(payload, scope('wrk_rel'))).toThrow()
    }
    expect(() => assertWorkspaceWorkDetail(base, {
      projectId: 'prj_other', workspaceId: 'w1', workItemId: 'wrk_rel',
    })).toThrow()
    expect(() => assertWorkspaceWorkDetail(base, {
      projectId: REG_P1, workspaceId: 'ws_other', workItemId: 'wrk_rel',
    })).toThrow()
  })

  it('working/completed 的 status-only payload 不得通过', () => {
    const base = detail('wrk_state').data
    const workingOnly = {
      ...base,
      work_item: { ...base.work_item, status: 'working' },
    }
    const completedOnly = {
      ...base,
      work_item: { ...base.work_item, status: 'completed' },
    }
    const activeClaimOnly = {
      ...base,
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_state', identity_id: 'idn_1',
        generation: 1, state: 'active', revision: 1,
      },
    }
    expect(() => assertWorkspaceWorkDetail(workingOnly, scope('wrk_state'))).toThrow()
    expect(() => assertWorkspaceWorkDetail(completedOnly, scope('wrk_state'))).toThrow()
    expect(() => assertWorkspaceWorkDetail(activeClaimOnly, scope('wrk_state'))).toThrow()
  })

  it('claim/reply/complete/failure 与 agent reply 必须绑定同一 claim principal', () => {
    const id = 'wrk_assoc'
    const base = detail(id, {
      thread: {
        ...detail(id).data.thread,
        messages: [detail(id).data.thread.messages[0], replyMessage(id)],
      },
      work_item: {
        ...detail(id).data.work_item, status: 'completed', revision: 3,
      },
      claim: {
        claim_id: 'clm_1', work_item_id: id, identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [
        delivery('succeeded'), claimReceipt(),
        messageReceipt(id, 'reply'), messageReceipt(id, 'complete'),
      ],
    }).data
    const bad = [
      {
        ...base,
        receipts: base.receipts.map((item: ReturnType<typeof claimReceipt>) => (
          item.kind === 'claim' ? { ...item, identity_id: 'idn_other' } : item
        )),
      },
      {
        ...base,
        receipts: base.receipts.map((item: ReturnType<typeof claimReceipt>) => (
          item.kind === 'reply' ? { ...item, claim_id: 'clm_other' } : item
        )),
      },
      {
        ...base,
        receipts: base.receipts.map((item: ReturnType<typeof claimReceipt>) => (
          item.kind === 'complete' ? { ...item, generation: 2 } : item
        )),
      },
      {
        ...base,
        thread: {
          ...base.thread,
          messages: [
            base.thread.messages[0],
            {
              ...base.thread.messages[1],
              author_ref: 'idn_other', author_generation: 2,
            },
          ],
        },
      },
      {
        ...base,
        receipts: [...base.receipts, {
          receipt_id: 'rct_failure_other', kind: 'failure', outcome: 'failed',
          reason: 'failed', evidence_digest: 'sha256:x',
          created_at: '2026-08-16T10:07:00Z', claim_id: 'clm_1',
          message_id: null, identity_id: 'idn_other', generation: 1,
        }],
      },
    ]
    for (const payload of bad) {
      expect(() => assertWorkspaceWorkDetail(payload, scope(id))).toThrow()
    }
  })

  it('非 delivery receipt outcome 必须匹配 kind 的冻结闭集', () => {
    const id = 'wrk_outcome'
    const completed = detail(id, {
      thread: {
        ...detail(id).data.thread,
        messages: [detail(id).data.thread.messages[0], replyMessage(id)],
      },
      work_item: {
        ...detail(id).data.work_item, status: 'completed', revision: 3,
      },
      claim: {
        claim_id: 'clm_1', work_item_id: id, identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [
        delivery('succeeded'), claimReceipt(),
        messageReceipt(id, 'reply'), messageReceipt(id, 'complete'),
      ],
    }).data
    for (const kind of ['claim', 'reply', 'complete']) {
      for (const outcome of ['failed', 'denied', '']) {
        const malformed = {
          ...completed,
          receipts: completed.receipts.map((receipt: { kind: string }) => (
            receipt.kind === kind ? { ...receipt, outcome } : receipt
          )),
        }
        expect(() => assertWorkspaceWorkDetail(malformed, scope(id))).toThrow()
      }
    }

    const failed = detail(id, {
      work_item: {
        ...detail(id).data.work_item, status: 'failed', revision: 3,
      },
      claim: {
        claim_id: 'clm_1', work_item_id: id, identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [claimReceipt(), {
        receipt_id: 'rct_failure_outcome', kind: 'failure', outcome: 'failed',
        reason: 'failed', evidence_digest: 'sha256:x',
        created_at: '2026-08-16T10:07:00Z', claim_id: 'clm_1',
        message_id: null, identity_id: 'idn_1', generation: 1,
      }],
    }).data
    for (const outcome of ['ok', 'denied', '']) {
      const malformed = {
        ...failed,
        receipts: failed.receipts.map((receipt: { kind: string }) => (
          receipt.kind === 'failure' ? { ...receipt, outcome } : receipt
        )),
      }
      expect(() => assertWorkspaceWorkDetail(malformed, scope(id))).toThrow()
    }
  })

  it('status=failed 没有匹配 failure receipt 时 fail-closed', () => {
    const base = detail('wrk_failed_only').data
    expect(() => assertWorkspaceWorkDetail({
      ...base,
      work_item: { ...base.work_item, status: 'failed', revision: 2 },
    }, scope('wrk_failed_only'))).toThrow()
  })

  it('getWorkspaceWorkDetail 走 GET detail 端点并解析', async () => {
    const fetchMock = stubFetch({
      ...defaultFetchMap(),
      [`${LIST_URL}/wrk_c`]: { data: detail('wrk_c').data, meta },
    })
    window.localStorage.clear()
    const result = await getWorkspaceWorkDetail(REG_P1, 'w1', 'wrk_c')
    expect(result.data.work_item.work_item_id).toBe('wrk_c')
    expect(fetchMock).toHaveBeenCalled()
  })
})

describe('deriveExecutionTimeline 状态推导', () => {
  const root = detail('wrk_t')

  it('错误 outcome 即使绕过 DTO parser 也不得按 receipt kind 补造时间线', () => {
    const deniedClaim = detail('wrk_t', {
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [{ ...claimReceipt(), outcome: 'denied' }],
    }).data
    expect(deriveExecutionTimeline(deniedClaim).timeline).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ label: '已领取' })]),
    )

    const okFailure = detail('wrk_t', {
      work_item: { ...root.data.work_item, status: 'failed', revision: 3 },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
      receipts: [{
        receipt_id: 'rct_failure_wrong', kind: 'failure', outcome: 'ok',
        reason: 'not durable', evidence_digest: 'sha256:x',
        created_at: '2026-08-16T10:07:00Z', claim_id: 'clm_1',
        message_id: null, identity_id: 'idn_1', generation: 1,
      }],
    }).data
    const model = deriveExecutionTimeline(okFailure)
    expect(model.phase).not.toBe('failed')
    expect(model.timeline).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ label: '失败' })]),
    )
  })

  it('未派遣：无 delivery 且 unassigned → 未开始，不显示任何执行状态', () => {
    const model = deriveExecutionTimeline(root.data)
    expect(hasExecutionStarted(root.data)).toBe(false)
    expect(model.timeline).toEqual([])
  })

  it('等待领取：unassigned + delivery succeeded', () => {
    const payload = detail('wrk_t', {
      receipts: [delivery('succeeded')],
      work_item: { ...root.data.work_item, status: 'unassigned', revision: 2 },
    }).data
    const model = deriveExecutionTimeline(payload)
    expect(model.phase).toBe('delivered')
    expect(model.timeline[0].label).toBe('已派遣')
  })

  it('intent-only 只显示派遣确认中，不显示等待领取', () => {
    const payload = detail('wrk_t', { receipts: [delivery('intent')] }).data
    const model = deriveExecutionTimeline(payload)
    expect(model.phase).toBe('dispatching')
    expect(model.timeline.map((entry) => entry.detail)).not.toContain('等待成员领取')
  })

  it('working 只来自 status=working，绝不伪造', () => {
    const delivered = detail('wrk_t', { receipts: [delivery('succeeded')] }).data
    expect(deriveExecutionTimeline(delivered).phase).not.toBe('working')
    const working = detail('wrk_t', {
      receipts: [delivery('succeeded'), claimReceipt()],
      work_item: { ...root.data.work_item, status: 'working', revision: 3 },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'active', revision: 1,
      },
    }).data
    expect(deriveExecutionTimeline(working).phase).toBe('working')
  })

  it('typed failure：status=failed 必须有同 claim principal 的 failure receipt', () => {
    const failed = detail('wrk_t', {
      receipts: [
        delivery('succeeded'), claimReceipt(),
        {
          receipt_id: 'rct_f_1', kind: 'failure', outcome: 'failed',
          reason: '写门拒绝', evidence_digest: 'sha256:x',
          created_at: '2026-08-16T10:04:00Z', claim_id: 'clm_1',
          message_id: null, identity_id: 'idn_1', generation: 1,
        },
      ],
      work_item: { ...root.data.work_item, status: 'failed', revision: 4 },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
    }).data
    const model = deriveExecutionTimeline(
      assertWorkspaceWorkDetail(failed, scope('wrk_t')),
    )
    expect(model.phase).toBe('failed')
    expect(model.failureReason).toBe('写门拒绝')
  })

  it('reply + completed：agent 正文进入回复区', () => {
    const done = detail('wrk_t', {
      thread: {
        ...root.data.thread,
        messages: [root.data.thread.messages[0], replyMessage('wrk_t')],
      },
      receipts: [
        delivery('succeeded'), claimReceipt(),
        {
          receipt_id: 'rct_r_1', kind: 'reply', outcome: 'ok', reason: null,
          evidence_digest: 'sha256:x', created_at: '2026-08-16T10:05:00Z',
          claim_id: 'clm_1', message_id: 'msg_reply_wrk_t',
          identity_id: 'idn_1', generation: 1,
        },
        {
          receipt_id: 'rct_p_1', kind: 'complete', outcome: 'ok', reason: null,
          evidence_digest: 'sha256:x', created_at: '2026-08-16T10:06:00Z',
          claim_id: 'clm_1', message_id: 'msg_reply_wrk_t',
          identity_id: 'idn_1', generation: 1,
        },
      ],
      work_item: { ...root.data.work_item, status: 'completed', revision: 5 },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
    }).data
    const model = deriveExecutionTimeline(done)
    expect(model.phase).toBe('completed')
    expect(model.agentReplies.map((item) => item.body)).toEqual(['成员回复正文'])
    expect(model.timeline.map((entry) => entry.label)).toEqual([
      '已派遣', '已领取', '已回复', '已完成',
    ])
  })

  it('派遣结果未知：unassigned + delivery outcome_unknown', () => {
    const unknown = detail('wrk_t', { receipts: [delivery('outcome_unknown')] }).data
    const model = deriveExecutionTimeline(unknown)
    expect(model.phase).toBe('delivery_unknown')
    expect(model.timeline[0].label).toBe('派遣结果未知')
  })

  it('未派遣任务不轮询；有 delivery 或 active working 才轮询', () => {
    expect(executionTimelineRefetchInterval(root.data)).toBe(false)
    expect(executionTimelineRefetchInterval(
      detail('wrk_t', { receipts: [delivery('intent')] }).data,
    )).toBe(5000)
    expect(executionTimelineRefetchInterval(detail('wrk_t', {
      work_item: { ...root.data.work_item, status: 'working' },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_t', identity_id: 'idn_1',
        generation: 1, state: 'active', revision: 1,
      },
    }).data)).toBe(5000)
  })
})

describe('Focus 页执行时间线行为', () => {
  function stubWorld(details: Record<string, unknown>, list: unknown[]) {
    return stubFetch((url) => {
      const match = url.match(/\/work-items\/(wrk_[a-z0-9]+)$/)
      if (match && match[1] in details) {
        return { body: { data: details[match[1]], meta } }
      }
      if (url === LIST_URL || url.startsWith(`${LIST_URL}?`)) {
        return { body: { data: { items: list, next_cursor: null }, meta } }
      }
      const key = Object.keys(defaultFetchMap())
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      if (key) return { body: (defaultFetchMap() as Record<string, unknown>)[key] }
      return undefined
    })
  }

  it.each([
    ['unassigned', '未分配'],
    ['working', '工作中'],
    ['completed', '已完成'],
    ['failed', '失败'],
  ] as const)('列表与选中任务按 %s 显示冻结标签 %s', async (status, label) => {
    window.localStorage.clear()
    const id = `wrk_${status}`
    const body = `状态任务-${status}`
    stubWorld({}, [aggregate(id, body, status)])
    renderApp(`${HOME}?work=${id}`)

    const task = await screen.findByTitle(body)
    expect(task).toHaveTextContent(label)
    const savedMeta = document.querySelector('.focus-task-meta')
    expect(savedMeta).toHaveTextContent(label)
    if (status !== 'unassigned') {
      const preparation = await screen.findByRole('region', { name: '执行准备' })
      expect(within(preparation).getByText(label)).toBeVisible()
      expect(within(preparation).queryAllByRole('button')).toHaveLength(0)
      expect(within(preparation).queryByRole('textbox')).not.toBeInTheDocument()
      expect(within(preparation).queryByRole('radio')).not.toBeInTheDocument()
    }
  })

  it('已派遣任务显示等待领取时间线；切换任务与 ?work 保持；无执行任务不渲染', async () => {
    window.localStorage.clear()
    const list = [
      aggregate('wrk_a', '任务A'),
      aggregate('wrk_b', '任务B'),
    ]
    stubWorld(
      {
        wrk_a: detail('wrk_a', { receipts: [delivery('succeeded')] }).data,
        wrk_b: detail('wrk_b').data,
      },
      list,
    )
    renderApp(`${HOME}?work=wrk_a`)

    const timeline = await screen.findByRole('region', { name: '执行时间线' })
    expect(await within(timeline).findByText('已派遣，等待领取')).toBeVisible()
    expect(within(timeline).getByText('等待成员领取')).toBeVisible()
    expect(within(timeline).queryByText('成员工作中')).not.toBeInTheDocument()

    // 切到未派遣任务：时间线不渲染
    fireEvent.click(screen.getAllByTitle('任务A')[0].closest('button') ? screen.getByTitle('任务B') : screen.getByTitle('任务B'))
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: '执行时间线' })).not.toBeInTheDocument()
    })
    expect(screen.queryByText('已派遣，等待领取')).not.toBeInTheDocument()

    // 切回 wrk_a：?work 恢复且时间线仍在（缓存/重取均可）
    fireEvent.click(screen.getByTitle('任务A'))
    expect(
      await within(await screen.findByRole('region', { name: '执行时间线' }))
        .findByText('已派遣，等待领取'),
    ).toBeVisible()
  })

  it('刷新（重挂载）后 completed 任务的 reply 正文与时间线保持', async () => {
    window.localStorage.clear()
    const done = detail('wrk_done', {
      thread: {
        ...detail('wrk_done').data.thread,
        messages: [
          detail('wrk_done').data.thread.messages[0],
          replyMessage('wrk_done'),
        ],
      },
      receipts: [
        delivery('succeeded'), claimReceipt(),
        messageReceipt('wrk_done', 'reply'), messageReceipt('wrk_done', 'complete'),
      ],
      work_item: {
        ...detail('wrk_done').data.work_item, status: 'completed', revision: 5,
      },
      claim: {
        claim_id: 'clm_1', work_item_id: 'wrk_done', identity_id: 'idn_1',
        generation: 1, state: 'closed', revision: 2,
      },
    }).data
    stubWorld({ wrk_done: done }, [aggregate('wrk_done', '结果任务', 'completed')])
    const first = renderApp(`${HOME}?work=wrk_done`)
    const timeline = await screen.findByRole('region', { name: '执行时间线' })
    await waitFor(() => {
      expect(within(timeline).getByRole('status')).toHaveTextContent('已完成')
    })
    expect(await within(timeline).findByText('成员回复正文')).toBeVisible()
    expect(within(timeline).getByText('已领取')).toBeVisible()
    expect(within(timeline).getByText('已回复')).toBeVisible()
    expect(screen.getByTitle('结果任务')).toHaveTextContent('已完成')
    expect(document.querySelector('.focus-task-meta')).toHaveTextContent('已完成')
    expect(within(screen.getByRole('region', { name: '执行准备' })).queryAllByRole('button'))
      .toHaveLength(0)
    first.unmount()

    const second = renderApp(`${HOME}?work=wrk_done`)
    const timeline2 = await screen.findByRole('region', { name: '执行时间线' })
    await waitFor(() => {
      expect(within(timeline2).getByRole('status')).toHaveTextContent('已完成')
    })
    expect(await within(timeline2).findByText('成员回复正文')).toBeVisible()
    expect(screen.getByTitle('结果任务')).toHaveTextContent('已完成')
    expect(document.querySelector('.focus-task-meta')).toHaveTextContent('已完成')
    expect(within(screen.getByRole('region', { name: '执行准备' })).queryAllByRole('button'))
      .toHaveLength(0)
    second.unmount()
  })

  it('detail 读取失败显示诚实降态，不伪造执行状态', async () => {
    window.localStorage.clear()
    stubFetch((url) => {
      const match = url.match(/\/work-items\/(wrk_[a-z0-9]+)$/)
      if (match) {
        return {
          status: 503,
          body: {
            error: {
              code: 'store_read_failed', message: 'store read failed',
              retryable: true, request_id: 'req_1', details: {},
            },
          },
        }
      }
      if (url === LIST_URL) {
        return {
          body: { data: { items: [aggregate('wrk_x', '任务X')], next_cursor: null }, meta },
        }
      }
      const key = Object.keys(defaultFetchMap())
        .filter((k) => url === k || url.startsWith(`${k}?`))
        .sort((a, b) => b.length - a.length)[0]
      if (key) return { body: (defaultFetchMap() as Record<string, unknown>)[key] }
      return undefined
    })
    renderApp(`${HOME}?work=wrk_x`)
    expect(
      await screen.findByText('执行时间线暂不可用', {}, { timeout: 5000 }),
    ).toBeVisible()
    expect(screen.queryByText('成员工作中')).not.toBeInTheDocument()
    expect(screen.queryByText('已派遣，等待领取')).not.toBeInTheDocument()
  })

  it('任意 ApiError status=404 的列表-详情竞态均不渲染时间线', async () => {
    window.localStorage.clear()
    stubFetch((url) => {
      if (url.endsWith('/work-items/wrk_gone')) {
        return {
          status: 404,
          body: {
            error: {
              code: 'project_not_found', message: 'project not found',
              retryable: false, request_id: 'req_404', details: {},
            },
          },
        }
      }
      if (url === LIST_URL) {
        return {
          body: {
            data: { items: [aggregate('wrk_gone', '已删除任务')], next_cursor: null }, meta,
          },
        }
      }
      const key = Object.keys(defaultFetchMap())
        .filter((item) => url === item || url.startsWith(`${item}?`))
        .sort((left, right) => right.length - left.length)[0]
      if (key) return { body: (defaultFetchMap() as Record<string, unknown>)[key] }
      return undefined
    })
    renderApp(`${HOME}?work=wrk_gone`)
    await screen.findByText('已删除任务')
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: '执行时间线' })).not.toBeInTheDocument()
    })
    expect(screen.queryByText('执行时间线暂不可用')).not.toBeInTheDocument()
  })

  it('时间线不暴露 transcript/Pane/digest 技术证据', async () => {
    window.localStorage.clear()
    const payload = detail('wrk_s', { receipts: [delivery('succeeded')] }).data
    stubWorld({ wrk_s: payload }, [aggregate('wrk_s', '任务S')])
    renderApp(`${HOME}?work=wrk_s`)
    const timeline = await screen.findByRole('region', { name: '执行时间线' })
    const text = timeline.textContent ?? ''
    for (const forbidden of ['sha256', 'transcript', 'pane', 'argv', 'token', 'rct_']) {
      expect(text.includes(forbidden)).toBe(false)
    }
  })
})
