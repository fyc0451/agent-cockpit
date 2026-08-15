import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { createAgent, getAgent, sendAgentPrompt, SUPPORTED_AGENT_KINDS, type AgentView } from '../api/agents'
import { newIdempotencyKey } from '../api/idempotency'
import { useLegacyEnvCheck } from '../api/localSlice'
import type { Project, Workspace } from '../api/types'
import { routeHrefs } from '../app/routes'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { clearRecentAgent, lookupRecentAgent, rememberRecentAgent } from '../state/recentAgent'

/** 状态词 → 用户语言（不直出内部状态机细节；闭集见 agents.ts AGENT_STATUSES） */
const STATUS_LABEL: Record<string, string> = {
  idle: '空闲',
  working: '正在执行',
  blocked: '需要你处理',
  done: '已完成',
  unknown: '状态暂不可用',
}

/** 空 composer 的无副作用示例：只一键填入，不自动发送 */
const EXAMPLE_PROMPT = '概览这个项目，并说明主要目录的作用'

/**
 * 后端 R3 public codes → 用户语言（code 不作主文案）；未知码回退 server message。
 * 四个 start 类 code 后端保证同 key 可重验：显示后可点主按钮直接重试（同一 key）。
 * agent_send_outcome_unknown（409/nonretryable）与 agent_not_found（404）走专门流程，不在此表。
 */
const AGENT_ERROR_LABEL: Record<string, string> = {
  workspace_agent_unavailable: '这台工作空间暂时无法启动 Agent，请稍后重试',
  workspace_agent_cleanup_incomplete: '上一个 Agent 会话还在清理，请稍后重试',
  agent_start_failed: 'Agent 启动失败，请再试一次',
  agent_start_cleanup_incomplete: '启动前的清理还没完成，请稍后重试',
  not_found: '没有找到这个 Agent 会话，可以开始新任务',
  forbidden: '当前没有权限执行这个操作',
  conflict: '操作与当前状态冲突，请刷新后重试',
  revision_conflict: '页面数据已过期，请刷新后重试',
  invalid_argument: '请求内容不被接受，请修改后重试',
  disconnected: '无法连接服务，请确认 Cockpit 正在运行',
  server_error: '服务暂时出现问题，请稍后重试',
  http_error: '请求失败，请稍后重试',
  protocol_error: '服务返回了无法识别的数据，请稍后重试',
}

function agentErrorMessage(err: unknown): string {
  if (err instanceof ApiError) return AGENT_ERROR_LABEL[err.code] ?? err.message
  return '操作失败，请再试一次'
}

/** 有界刷新窗口：发送后即使首响应 idle 也持续取状态，直到 transcript 变化/明确完成/超时 */
const POLL_INTERVAL_MS = 1000
const POLL_MAX_TICKS = 30

function AgentBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const projectId = project.project_id ?? ''
  const workspaceId = workspace.workspace_id ?? workspace.id ?? ''

  const envCheck = useLegacyEnvCheck()
  const [searchParams, setSearchParams] = useSearchParams()
  const agentParam = searchParams.get('agent')

  const [agent, setAgent] = useState<AgentView | null>(null)
  const [restoring, setRestoring] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const [kind, setKind] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [pending, setPending] = useState<'create' | 'prompt' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [waiting, setWaiting] = useState(false)
  const [pollExhausted, setPollExhausted] = useState(false)
  /** 发送结果待确认（agent_send_outcome_unknown）：不自动重发，只有显式确认动作才换新 key */
  const [sendUncertain, setSendUncertain] = useState<{
    agentId: string
    prompt: string
    baseline: string
  } | null>(null)
  /** 会话已断开（agent_not_found）：旧 agent 与 query 已清，任务文本保留 */
  const [goneNotice, setGoneNotice] = useState(false)

  // intent 冻结：同 intent 重试原样复用幂等键（byte-equivalent），成功/换内容才重建
  const createIntentRef = useRef<{ kind: string; key: string } | null>(null)
  const promptIntentRef = useRef<{ prompt: string; key: string } | null>(null)
  const pollTimerRef = useRef<number | null>(null)
  const pollGenRef = useRef(0)
  const baselineRef = useRef('')
  const sendUncertainRef = useRef(sendUncertain)

  const setUncertain = (v: typeof sendUncertain) => {
    sendUncertainRef.current = v
    setSendUncertain(v)
  }

  /** 刷新/轮询观察到 transcript 增长或转 working → 发送实际已到达，解除待确认 */
  const resolveUncertainIfArrived = (view: AgentView) => {
    const u = sendUncertainRef.current
    if (u && (view.transcript !== u.baseline || view.status === 'working')) setUncertain(null)
  }

  const stopPolling = useCallback(() => {
    pollGenRef.current += 1
    if (pollTimerRef.current != null) {
      window.clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
    setWaiting(false)
  }, [])

  useEffect(() => stopPolling, [stopPolling])

  /** agent_not_found（任一路径）：停轮询、清旧 agent 与 query 与最近记录（杜绝继续 POST 旧 agent、
   *  禁止恢复循环）、保留任务文本，给出「Agent 会话已断开 / 开始新任务」出口 */
  const onAgentGone = useCallback(() => {
    stopPolling()
    setAgent(null)
    setUncertain(null)
    setActionError(null)
    setRestoreError(null)
    setPollExhausted(false)
    setGoneNotice(true)
    clearRecentAgent(projectId, workspaceId)
    setSearchParams({}, { replace: true })
  }, [stopPolling, setSearchParams, projectId, workspaceId])

  /** 有界轮询 tick 停止语义：
   *  - working：无论 transcript 是否变化（部分输出）都继续，绝不早停；
   *  - idle：仅 transcript 相对本轮 baseline 已变化才停，未变化继续；
   *  - done/blocked/其他明确状态：停。
   *  次数耗尽：仍在执行才提示超时（idle 静默停，页面有常驻手动刷新入口） */
  const pollTick = useCallback(
    (agentId: string, ticksLeft: number) => {
      const gen = pollGenRef.current
      pollTimerRef.current = window.setTimeout(() => {
        pollTimerRef.current = null
        void getAgent({ projectId, workspaceId, agentId })
          .then((view) => {
            if (gen !== pollGenRef.current) return
            setAgent(view)
            resolveUncertainIfArrived(view)
            const settled =
              (view.status !== 'idle' && view.status !== 'working') ||
              (view.status === 'idle' && view.transcript !== baselineRef.current)
            if (settled) {
              stopPolling()
              return
            }
            if (ticksLeft <= 1) {
              stopPolling()
              if (view.status === 'working') setPollExhausted(true)
              return
            }
            pollTick(agentId, ticksLeft - 1)
          })
          .catch((err) => {
            if (gen !== pollGenRef.current) return
            if (err instanceof ApiError && err.code === 'agent_not_found') {
              onAgentGone()
              return
            }
            if (ticksLeft <= 1) {
              stopPolling()
              return
            }
            pollTick(agentId, ticksLeft - 1)
          })
      }, POLL_INTERVAL_MS)
    },
    [projectId, workspaceId, stopPolling, onAgentGone],
  )

  const startPollWindow = useCallback(
    (agentId: string, baseline: string) => {
      stopPolling()
      baselineRef.current = baseline
      setPollExhausted(false)
      setWaiting(true)
      pollTick(agentId, POLL_MAX_TICKS)
    },
    [pollTick, stopPolling],
  )

  // 刷新恢复：URL ?agent= 存在且本地无会话时 GET 恢复；
  // status=idle/working 时自动进入有界刷新窗口（快速竞态下不丢回复）
  useEffect(() => {
    if (!agentParam || agent != null) return
    let cancelled = false
    setRestoring(true)
    setRestoreError(null)
    getAgent({ projectId, workspaceId, agentId: agentParam })
      .then((view) => {
        if (cancelled) return
        setAgent(view)
        setRestoring(false)
        rememberRecentAgent(projectId, workspaceId, view.agent_id)
        if (view.status === 'idle' || view.status === 'working') {
          startPollWindow(view.agent_id, view.transcript)
        }
      })
      .catch((err) => {
        if (cancelled) return
        setRestoring(false)
        if (err instanceof ApiError && err.code === 'agent_not_found') {
          onAgentGone()
          return
        }
        setRestoreError(agentErrorMessage(err))
      })
    return () => {
      cancelled = true
    }
  }, [agentParam, agent, projectId, workspaceId, startPollWindow, onAgentGone])

  // P1-b：URL 无 ?agent= 时自动转到该工作区最近一次成功创建/恢复的会话；
  // 记录失效由 onAgentGone/onStartNew 清除，不会形成恢复循环
  useEffect(() => {
    if (agentParam || agent != null) return
    const recent = lookupRecentAgent(projectId, workspaceId)
    if (recent) setSearchParams({ agent: recent }, { replace: true })
  }, [agentParam, agent, projectId, workspaceId, setSearchParams])

  // 可提交类型 = 已安装 ∩ 本轮支持白名单（已安装但不支持的类型不进选项，避免 400）
  const installedAll = envCheck.data
    ? Object.entries(envCheck.data.agents)
        .filter(([, item]) => item.installed)
        .map(([name]) => name)
    : []
  const installedKinds = installedAll.filter((name) =>
    (SUPPORTED_AGENT_KINDS as readonly string[]).includes(name),
  )
  const selectedKind = kind ?? installedKinds[0] ?? null

  /** 发送 prompt（自带错误分流）：成功清输入并开有界窗口（baseline=POST 返回 transcript）；
   *  agent_not_found → 断开流程；agent_send_outcome_unknown → 待确认（不换 key 不自动重发）；
   *  其他 → 用户语言错误，主按钮同 key 直接重试 */
  const doPrompt = async (current: AgentView, text: string) => {
    if (promptIntentRef.current?.prompt !== text) {
      promptIntentRef.current = { prompt: text, key: newIdempotencyKey() }
    }
    const pIntent = promptIntentRef.current
    setPending('prompt')
    try {
      const next = await sendAgentPrompt(
        { projectId, workspaceId, agentId: current.agent_id },
        text,
        pIntent.key,
      )
      promptIntentRef.current = null
      setAgent(next)
      setPrompt('')
      startPollWindow(next.agent_id, next.transcript)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'agent_not_found') {
        onAgentGone()
      } else if (err instanceof ApiError && err.code === 'agent_send_outcome_unknown') {
        setUncertain({ agentId: current.agent_id, prompt: text, baseline: current.transcript })
      } else {
        setActionError(agentErrorMessage(err))
      }
    } finally {
      setPending(null)
    }
  }

  const onSend = () => {
    const text = prompt.trim()
    // 待确认期间主按钮不可造成重发
    if (!text || pending || sendUncertainRef.current) return
    setActionError(null)
    void (async () => {
      let current = agent
      if (!current) {
        // 首次：start-or-attach（幂等键冻结，四个 start 类 code 同 key 可重验），随后发送
        if (!selectedKind) return
        if (createIntentRef.current?.kind !== selectedKind) {
          createIntentRef.current = { kind: selectedKind, key: newIdempotencyKey() }
        }
        const intent = createIntentRef.current
        setPending('create')
        try {
          current = await createAgent(projectId, workspaceId, selectedKind, intent.key)
        } catch (err) {
          setActionError(agentErrorMessage(err))
          setPending(null)
          return
        }
        createIntentRef.current = null
        setAgent(current)
        // agent_id 落 URL query：刷新后 GET 恢复，不依赖仅内存状态；并记为最近会话（P1-b）
        setSearchParams({ agent: current.agent_id }, { replace: true })
        rememberRecentAgent(projectId, workspaceId, current.agent_id)
      }
      await doPrompt(current, text)
    })()
  }

  /** 待确认的显式二次动作：只有用户点击才生成新 prompt key 并重发 */
  const onConfirmResend = () => {
    const u = sendUncertainRef.current
    if (!u || pending) return
    promptIntentRef.current = null
    setUncertain(null)
    setActionError(null)
    const current = agent
    if (!current || current.agent_id !== u.agentId) return
    void doPrompt(current, u.prompt)
  }

  /** 手动刷新（常驻入口）：立即 GET 一次；idle/working 则续一个有界窗口等新回复 */
  const onRefresh = () => {
    const current = agent
    if (!current || pending) return
    setActionError(null)
    setPollExhausted(false)
    void (async () => {
      try {
        const view = await getAgent({ projectId, workspaceId, agentId: current.agent_id })
        setAgent(view)
        resolveUncertainIfArrived(view)
        if (view.status === 'idle' || view.status === 'working') {
          startPollWindow(view.agent_id, view.transcript)
        } else {
          stopPolling()
        }
      } catch (err) {
        if (err instanceof ApiError && err.code === 'agent_not_found') {
          onAgentGone()
        } else {
          setActionError(agentErrorMessage(err))
        }
      }
    })()
  }

  const onStartNew = () => {
    stopPolling()
    setAgent(null)
    setRestoreError(null)
    setActionError(null)
    setPollExhausted(false)
    setUncertain(null)
    setGoneNotice(false)
    clearRecentAgent(projectId, workspaceId)
    setSearchParams({}, { replace: true })
  }

  const mainLabel = pending ? '正在发送…' : agent ? '发送' : '开始任务'
  const canSend =
    !pending && !sendUncertain && prompt.trim() !== '' && (agent != null || selectedKind != null)
  const sendTitle = sendUncertain
    ? '发送结果待确认：请先刷新确认，或显式选择重新发送'
    : canSend
      ? undefined
      : '先写下要让 Agent 做的事'

  return (
    <>
      <PageHeader title="Agent" sub={workspace.name ?? workspace.id} />
      {envCheck.isPending ? (
        <StatusState kind="loading" banner title="正在检查可用的 Agent…" />
      ) : envCheck.isError ? (
        <QueryErrorState error={envCheck.error} onRetry={() => void envCheck.refetch()} />
      ) : restoring ? (
        <StatusState kind="loading" banner title="正在恢复会话…" />
      ) : (
        <>
          {restoreError ? (
            <StatusState
              kind="error"
              banner
              title="没有找到这个 Agent 会话"
              description={restoreError}
              action={{ label: '开始新任务', onClick: onStartNew }}
            />
          ) : null}
          {goneNotice ? (
            <StatusState
              kind="error"
              banner
              title="Agent 会话已断开"
              description="原来的会话已不在；你的任务文本还保留在输入框里，可以直接开始新任务。"
              action={{ label: '开始新任务', onClick: () => setGoneNotice(false) }}
            />
          ) : null}
          {installedKinds.length === 0 && !agent ? (
            installedAll.length > 0 ? (
              <StatusState
                kind="empty"
                title="当前没有受支持的 Agent"
                description={`这台电脑只安装了暂不受支持的 Agent（${installedAll.join('、')}）；本轮支持 codex、claude、kimi、opencode、grok——先安装其中之一，然后重新检查。`}
              >
                <div className="state-actions">
                  <a className="btn btn--primary" href={routeHrefs.doctor()}>
                    打开环境自检
                  </a>
                  <Button variant="secondary" onClick={() => void envCheck.refetch()}>
                    重新检查
                  </Button>
                </div>
              </StatusState>
            ) : (
              <StatusState
                kind="empty"
                title="还没有可用的 Agent"
                description="先安装一个受支持的 Agent CLI（如 codex、kimi），然后重新检查。"
              >
                <div className="state-actions">
                  <a className="btn btn--primary" href={routeHrefs.doctor()}>
                    打开环境自检
                  </a>
                  <Button variant="secondary" onClick={() => void envCheck.refetch()}>
                    重新检查
                  </Button>
                </div>
              </StatusState>
            )
          ) : (
            <>
              {!agent ? (
                <label className="agent-field">
                  <span className="agent-field-label">Agent 类型</span>
                  <select
                    className="input"
                    aria-label="Agent 类型"
                    value={selectedKind ?? ''}
                    onChange={(e) => setKind(e.target.value)}
                  >
                    {installedKinds.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <label className="agent-field">
                <span className="agent-field-label">任务</span>
                <textarea
                  className="input agent-prompt"
                  aria-label="任务"
                  rows={3}
                  value={prompt}
                  placeholder="写下要让 Agent 做的事"
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </label>
              {prompt.trim() === '' ? (
                <Button
                  variant="ghost"
                  className="agent-example"
                  title="只填入输入框，不会发送"
                  onClick={() => setPrompt(EXAMPLE_PROMPT)}
                >
                  示例：{EXAMPLE_PROMPT}
                </Button>
              ) : null}
              <div className="agent-actions">
                <Button
                  variant="primary"
                  disabled={!canSend}
                  title={sendTitle}
                  onClick={onSend}
                >
                  {mainLabel}
                </Button>
                {agent ? (
                  <Button variant="secondary" title="立即取回最新状态与回复" onClick={onRefresh}>
                    刷新
                  </Button>
                ) : null}
                {agent ? (
                  <Button
                    variant="secondary"
                    title="收起当前会话并开始全新任务（原会话不会被删除）"
                    onClick={onStartNew}
                  >
                    新任务
                  </Button>
                ) : null}
              </div>
              {sendUncertain ? (
                <StatusState
                  kind="conflict"
                  banner
                  title="发送结果待确认"
                  description="刚才的发送结果未能确认。请先点「刷新」确认 Agent 是否已收到；确认未收到后，再点下面的按钮重新发送。"
                >
                  <div className="state-actions">
                    <Button variant="secondary" onClick={onRefresh}>
                      刷新
                    </Button>
                    <Button variant="danger" onClick={onConfirmResend}>
                      确认未收到，重新发送
                    </Button>
                  </div>
                </StatusState>
              ) : null}
              {actionError ? (
                <StatusState
                  kind="degraded"
                  banner
                  title="任务没有发出去"
                  description={`${actionError}；可直接再点一次主按钮重试。`}
                />
              ) : null}
              {agent ? (
                <section className="panel">
                  <h2 className="panel-title">状态与回复</h2>
                  <p className="agent-status" data-testid="agent-status">
                    状态：{STATUS_LABEL[agent.status] ?? '状态未知'}
                    {waiting ? ' · 正在等待回复…' : ''}
                  </p>
                  {pollExhausted ? (
                    <StatusState
                      kind="stale"
                      banner
                      title="还在等待回复"
                      description="Agent 可能还在处理；点「刷新」再取一次最新状态。"
                      action={{ label: '刷新', onClick: onRefresh }}
                    />
                  ) : null}
                  {agent.transcript ? (
                    <pre className="agent-transcript" data-testid="agent-transcript">
                      {agent.transcript}
                    </pre>
                  ) : (
                    <p className="list-sub">还没有回复；发送任务后会显示在这里。</p>
                  )}
                </section>
              ) : null}
            </>
          )}
        </>
      )}
    </>
  )
}

export function AgentPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <AgentBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
