// WEB-003 Local registration wizard：三步向导（选择位置 → 选择目录 → 确认项目）。
// 状态机按 proj-registration-wizard-state-kimi §2 精简（8 主状态合并进单个 useReducer；
// PROBE_RESULT 六子态由 discovery 响应派生）。纪律：
// I1 从不提交绝对路径（只有 {node_id, root_id, path}）；I2 提交必须带 Idempotency-Key +
// expected_discovery_fingerprint，缺一按钮 disabled；I3 fail-closed 控件激活 0 请求；
// I4 取消零写请求；I5 409/412/503 原地 typed 表达，不 toast 假成功。

import { useEffect, useId, useReducer, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { newIdempotencyKey } from '../../api/idempotency'
import {
  useNodeDirectories,
  useNodeRoots,
  useProjectDiscovery,
  useRegisterProject,
  useRuntimeNodes,
} from '../../api/registry'
import type {
  DirectoryEntry,
  DiscoveryResultData,
  RegisterProjectRequest,
  RuntimeNode,
} from '../../api/registry'
import type { ResponseMeta } from '../../api/types'
import { routes } from '../../app/routes'
import { Button } from '../../components/Button'
import { QueryErrorState } from '../../components/QueryErrorState'
import { StatusState } from '../../components/StatusState'
import { Tag } from '../../components/Tag'
import { useDialog } from '../../components/useDialog'
import {
  projectScope,
  useCapability,
  useReportCapabilities,
  type CapabilityScope,
} from '../../state/capabilities'

const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/

function slugValid(slug: string): boolean {
  return SLUG_RE.test(slug) && !slug.includes('--')
}

function deriveSlug(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return s || 'project'
}

type Step = 'node' | 'dir' | 'probe'

interface DirectorySelection {
  name: string
  path: string
}

interface WizardState {
  step: Step
  nodeId: string | null
  rootId: string | null
  rootDisplayName: string | null
  path: string
  selected: DirectorySelection | null
  probe: DiscoveryResultData | null
  probeMeta: ResponseMeta | null
  probeRevision: number
  probeDegraded: boolean
  probeError: ApiError | null
  displayName: string
  slug: string
  /** 幂等绑定：{ 序列化 body, key } 成对；body 逐字节相同才允许复用 key */
  submitBinding: { serializedBody: string; idempotencyKey: string } | null
  submitting: boolean
  submitError: ApiError | null
  succeeded: { project_id: string; slug: string; displayName: string } | null
}

const initialState: WizardState = {
  step: 'node',
  nodeId: null,
  rootId: null,
  rootDisplayName: null,
  path: '',
  selected: null,
  probe: null,
  probeMeta: null,
  probeRevision: 0,
  probeDegraded: false,
  probeError: null,
  displayName: '',
  slug: '',
  submitBinding: null,
  submitting: false,
  submitError: null,
  succeeded: null,
}

type Action =
  | { type: 'reset' }
  | { type: 'select-node'; nodeId: string }
  | { type: 'reset-node' }
  | { type: 'select-root'; rootId: string; displayName: string }
  | { type: 'reset-root' }
  | { type: 'enter-dir'; path: string }
  | { type: 'up-dir' }
  | { type: 'select-dir'; entry: DirectoryEntry }
  | { type: 'probe-start' }
  | { type: 'probe-ok'; revision: number; result: DiscoveryResultData; meta: ResponseMeta | null; degraded: boolean }
  | { type: 'probe-err'; revision: number; error: ApiError }
  | { type: 'back-to-dir' }
  | { type: 'set-display-name'; value: string }
  | { type: 'set-slug'; value: string }
  | { type: 'submit-start'; binding: { serializedBody: string; idempotencyKey: string } }
  | { type: 'submit-ok'; project_id: string }
  | { type: 'submit-err'; error: ApiError }

function reducer(state: WizardState, action: Action): WizardState {
  switch (action.type) {
    case 'reset':
      return { ...initialState, probeRevision: state.probeRevision + 1 }
    case 'select-node':
      return { ...state, step: 'dir', nodeId: action.nodeId, rootId: null, rootDisplayName: null, path: '', selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'reset-node':
      // 从目录步返回位置步（roots 空态的恢复动作）；手动返回后不再自动跳过位置步
      return { ...state, step: 'node', nodeId: null, rootId: null, rootDisplayName: null, path: '', selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'select-root':
      return { ...state, rootId: action.rootId, rootDisplayName: action.displayName, path: '', selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'reset-root':
      return { ...state, rootId: null, rootDisplayName: null, path: '', selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'enter-dir':
      return { ...state, path: action.path, selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'up-dir': {
      const parent = state.path.includes('/') ? state.path.slice(0, state.path.lastIndexOf('/')) : ''
      return { ...state, path: parent, selected: null, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    }
    case 'select-dir':
      return { ...state, selected: action.entry, probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, submitBinding: null }
    case 'probe-start':
      // PROBING 在途停留在目录步（识别按钮 disabled，重复触发 0 请求）；probe 状态清空
      return {
        ...state,
        probe: null,
        probeMeta: null,
        probeRevision: state.probeRevision + 1,
        probeError: null,
        submitError: null,
        succeeded: null,
      }
    case 'probe-ok':
      if (action.revision !== state.probeRevision) return state
      return {
        ...state,
        step: 'probe',
        probe: action.result,
        probeMeta: action.meta,
        probeDegraded: action.degraded,
        probeError: null,
        // 新 probe 完成 → 旧幂等绑定作废
        submitBinding: null,
        displayName: state.selected?.name ?? '',
        slug: deriveSlug(state.selected?.name ?? ''),
      }
    case 'probe-err':
      if (action.revision !== state.probeRevision) return state
      return { ...state, step: 'probe', probe: null, probeMeta: null, probeError: action.error }
    case 'back-to-dir':
      // T6/T12/T13：回目录步；改选目录后必须重新 probe（旧指纹随 probe 清空作废）
      return { ...state, step: 'dir', probe: null, probeMeta: null, probeRevision: state.probeRevision + 1, probeError: null, submitError: null, submitBinding: null, selected: null }
    case 'set-display-name':
      return { ...state, displayName: action.value }
    case 'set-slug':
      return { ...state, slug: action.value }
    case 'submit-start':
      return { ...state, submitting: true, submitError: null, submitBinding: action.binding }
    case 'submit-ok':
      return {
        ...state,
        submitting: false,
        // 成功卡片展示用户提交值（slug/displayName）+ 服务端 project_id
        succeeded: { project_id: action.project_id, slug: state.slug, displayName: state.displayName },
      }
    case 'submit-err':
      return { ...state, submitting: false, submitError: action.error }
  }
}

function probeSubstate(probe: DiscoveryResultData): 'ALREADY_REGISTERED' | 'FINGERPRINT_MATCH' | 'PLAIN_DIR' | 'NEW_GIT' {
  if (probe.exact_match) return 'ALREADY_REGISTERED'
  if (probe.possible_projects.length > 0) return 'FINGERPRINT_MATCH'
  if (probe.vcs.kind === 'none') return 'PLAIN_DIR'
  return 'NEW_GIT'
}

function nodeDisabledReason(node: RuntimeNode): string | null {
  if (node.kind !== undefined && node.kind !== 'local') return '仅支持本机节点，该节点暂不可用'
  if (node.availability !== undefined && node.availability !== 'available')
    return node.reason ?? '节点离线/不可用'
  if (node.kind === undefined && node.node_id !== 'local') return '仅支持本机节点，该节点暂不可用'
  return null
}

function NodeCard({ node, onSelect }: { node: RuntimeNode; onSelect: (id: string) => void }) {
  const descId = useId()
  const reason = nodeDisabledReason(node)
  if (reason) {
    return (
      <div
        className="card card--disabled"
        aria-disabled="true"
        aria-describedby={descId}
        tabIndex={0}
        title={reason}
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            e.stopPropagation()
          }
        }}
      >
        <span className="card-label">{node.display_name}</span>
        {node.availability === 'offline' ? <Tag tone="danger">离线</Tag> : <Tag tone="neutral">{node.kind ?? 'remote'}</Tag>}
        <span id={descId} className="card-reason ellipsis">
          {reason}
        </span>
      </div>
    )
  }
  return (
    <button type="button" className="card" onClick={() => onSelect(node.node_id)}>
      <span className="card-label">{node.display_name}</span>
      <Tag tone="success">local</Tag>
    </button>
  )
}

const STEPS: { key: Step; label: string }[] = [
  { key: 'node', label: '1 选择位置' },
  { key: 'dir', label: '2 选择目录' },
  { key: 'probe', label: '3 确认项目' },
]

export function ProjectWizard({
  open,
  onClose,
  onRegistered,
}: {
  open: boolean
  onClose: () => void
  onRegistered: (p: { project_id: string; slug: string }) => void
}) {
  const ref = useRef<HTMLElement>(null)
  const capabilityNamespace = useId()
  const navigate = useNavigate()
  const [state, dispatch] = useReducer(reducer, initialState)
  const nodes = useRuntimeNodes()
  const roots = useNodeRoots(state.step === 'dir' ? state.nodeId : null)
  const dirs = useNodeDirectories(
    state.step === 'dir' && state.nodeId && state.rootId
      ? { node_id: state.nodeId, root_id: state.rootId, path: state.path }
      : null,
  )
  const discovery = useProjectDiscovery()
  const register = useRegisterProject()
  const probeScope = projectScope(
    `discovery:${capabilityNamespace}:${state.probeRevision}:${state.probe?.discovery_fingerprint ?? 'none'}`,
  )
  useReportCapabilities(state.probeMeta, probeScope)

  useDialog(ref, open, onClose)

  // 唯一代码位置只在首次进入目录步自动展开；用户手动「更换代码位置」后不再自动跳
  const autoRootRef = useRef(false)
  // 唯一可用节点只在首次自动跳过位置步；用户手动「返回选择位置」后不再自动跳
  const autoNodeRef = useRef(false)

  // 取消后重开从 NODE_SELECT 全新开始（不残留 fingerprint/idempotency key）
  useEffect(() => {
    if (open) {
      autoRootRef.current = false
      autoNodeRef.current = false
      dispatch({ type: 'reset' })
    }
  }, [open])

  // 恰好一个可用 local 节点（其余 disabled remote/offline 不算可选）：自动进入目录步
  useEffect(() => {
    if (!open || state.step !== 'node') return
    if (autoNodeRef.current) return
    const ns = nodes.data?.data.nodes
    if (!ns) return
    const usable = ns.filter((n) => nodeDisabledReason(n) == null)
    if (usable.length === 1) {
      autoNodeRef.current = true
      dispatch({ type: 'select-node', nodeId: usable[0].node_id })
    }
  }, [open, state.step, nodes.data])

  // 唯一代码位置：自动展开其目录列表
  useEffect(() => {
    if (!open || state.step !== 'dir' || state.nodeId == null || state.rootId != null) return
    if (autoRootRef.current) return
    const items = roots.data?.data.items
    if (items && items.length === 1) {
      autoRootRef.current = true
      dispatch({ type: 'select-root', rootId: items[0].root_id, displayName: items[0].display_name })
    }
  }, [open, state.step, state.nodeId, state.rootId, roots.data])

  if (!open) return null

  const startProbe = () => {
    if (!state.selected || discovery.isPending) return
    const revision = state.probeRevision + 1
    dispatch({ type: 'probe-start' })
    const locator = {
      node_id: state.nodeId!,
      root_id: state.rootId!,
      path: state.selected.path,
    }
    discovery.mutate(locator, {
      onSuccess: (res) => {
        const degraded =
          res.data.complete === false || res.data.warnings.length > 0 || res.meta?.partial === true
        dispatch({ type: 'probe-ok', revision, result: res.data, meta: res.meta, degraded })
      },
      onError: (err) => dispatch({ type: 'probe-err', revision, error: err as ApiError }),
    })
  }

  const submit = () => {
    if (!state.probe) return
    const req: RegisterProjectRequest = {
      display_name: state.displayName.trim(),
      slug: state.slug,
      goal: null,
      locator: state.probe.locator,
      expected_discovery_fingerprint: state.probe.discovery_fingerprint,
    }
    // 幂等绑定：body 逐字节相同 → 复用旧绑定（同 key 同 body 重试）；
    // 任何字段变化（slug/name/goal/locator/fingerprint）→ 生成新 UUID
    const serializedBody = JSON.stringify(req)
    const binding =
      state.submitBinding && state.submitBinding.serializedBody === serializedBody
        ? state.submitBinding
        : { serializedBody, idempotencyKey: newIdempotencyKey() }
    dispatch({ type: 'submit-start', binding })
    register.mutate(
      { req, idempotencyKey: binding.idempotencyKey },
      {
        onSuccess: (res) => {
          dispatch({ type: 'submit-ok', project_id: res.data.project_id })
          onRegistered({ project_id: res.data.project_id, slug: res.data.slug })
        },
        onError: (err) => dispatch({ type: 'submit-err', error: err as ApiError }),
      },
    )
  }

  const openExisting = (slug: string) => {
    onClose()
    navigate(routes.project.workbench(slug))
  }

  // 登记成功主按钮：进入该项目 Workbench，URL 合同 ?createWorkspace=1 携带
  // 「自动打开 Workspace 创建」意图（深链/刷新可重放，后半链消费）
  const openWorkbench = () => {
    if (!state.succeeded) return
    onClose()
    navigate(routes.project.workbench(state.succeeded.slug, { createWorkspace: true }))
  }

  const footer = (
    <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
      <Button variant="ghost" onClick={onClose}>
        取消
      </Button>
    </div>
  )

  return (
    <div className="overlay" onClick={onClose}>
      <section
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="添加项目"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="drawer-head">
          <h2 className="panel-title">添加项目</h2>
          <button type="button" className="btn btn--icon" aria-label="关闭" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="state-actions" style={{ justifyContent: 'flex-start', marginTop: 0 }}>
          {STEPS.map((s) => (
            <Tag key={s.key} tone={state.step === s.key ? 'accent' : 'neutral'}>
              {s.label}
            </Tag>
          ))}
        </div>

        {state.step === 'node' ? (
          nodes.isPending ? (
            <StatusState kind="loading" title="正在加载节点…" />
          ) : nodes.isError ? (
            <QueryErrorState error={nodes.error} onRetry={() => nodes.refetch()} />
          ) : nodes.data!.data.nodes.length === 0 ? (
            <StatusState
              kind="empty"
              title="没有发现可用的位置"
              description="暂时没有可选择的计算位置；可以重新检查，或取消后稍后再试。"
            >
              <div className="state-actions">
                <Button variant="primary" onClick={() => void nodes.refetch()}>
                  重新检查
                </Button>
              </div>
            </StatusState>
          ) : (
            <div className="card-grid">
              {nodes.data!.data.nodes.map((n) => (
                <NodeCard key={n.node_id} node={n} onSelect={(id) => dispatch({ type: 'select-node', nodeId: id })} />
              ))}
            </div>
          )
        ) : null}

        {state.step === 'dir' ? (
          !state.rootId ? (
            roots.isPending ? (
              <StatusState kind="loading" title="正在加载根目录…" />
            ) : roots.isError ? (
              <QueryErrorState error={roots.error} onRetry={() => roots.refetch()} />
            ) : roots.data!.data.items.length === 0 ? (
              <StatusState
                kind="empty"
                title="这个位置下没有代码位置"
                description="可以重新检查，或返回重新选择位置。"
              >
                <div className="state-actions">
                  <Button variant="primary" onClick={() => void roots.refetch()}>
                    重新检查
                  </Button>
                  <Button variant="secondary" onClick={() => dispatch({ type: 'reset-node' })}>
                    返回选择位置
                  </Button>
                </div>
              </StatusState>
            ) : (
              <ul className="list project-wizard-list" aria-label="代码位置" tabIndex={0}>
                {roots.data!.data.items.map((r) => (
                  <li key={r.root_id} className="list-row">
                    <button
                      type="button"
                      className="drawer-item"
                      data-root-id={r.root_id}
                      onClick={() => dispatch({ type: 'select-root', rootId: r.root_id, displayName: r.display_name })}
                    >
                      {r.display_name}
                    </button>
                  </li>
                ))}
              </ul>
            )
          ) : (
            <>
              <div className="state-actions" style={{ justifyContent: 'flex-start', marginTop: 0 }}>
                <Button variant="ghost" onClick={() => dispatch({ type: 'reset-root' })}>
                  更换代码位置
                </Button>
                {state.path !== '' ? (
                  <Button variant="ghost" onClick={() => dispatch({ type: 'up-dir' })}>
                    上级目录
                  </Button>
                ) : null}
                <Tag tone="neutral">{state.path === '' ? '/' : state.path}</Tag>
              </div>
              {dirs.isPending ? (
                <StatusState kind="loading" title="正在加载目录…" />
              ) : dirs.isError ? (
                <QueryErrorState error={dirs.error} onRetry={() => dirs.refetch()} />
              ) : dirs.data!.data.entries.length === 0 ? (
                <StatusState
                  kind="empty"
                  title="这里还没有可选择的项目目录"
                  description="可以重新检查、返回上级目录或更换代码位置。"
                >
                  <div className="state-actions">
                    <Button variant="primary" onClick={() => void dirs.refetch()}>
                      重新检查
                    </Button>
                    {state.path !== '' ? (
                      <Button variant="secondary" onClick={() => dispatch({ type: 'up-dir' })}>
                        上级目录
                      </Button>
                    ) : null}
                    <Button variant="secondary" onClick={() => dispatch({ type: 'reset-root' })}>
                      更换代码位置
                    </Button>
                  </div>
                </StatusState>
              ) : (
                <>
                  {dirs.data!.data.partial || dirs.data!.data.warnings.length > 0 ? (
                    // B2：partial 时本地目录仍渲染；registered_project=null 语义是「未知」
                    <StatusState
                      kind="degraded"
                      banner
                      title="部分数据源不可用"
                      description="暂时无法确认目录是否已添加；可继续浏览和选择目录，稍后重试确认。"
                    />
                  ) : null}
                  <ul
                    className="list project-wizard-list"
                    aria-label={`目录 ${state.path === '' ? '/' : state.path}`}
                    tabIndex={0}
                  >
                    {dirs.data!.data.entries.map((entry) => (
                      <li key={entry.path} className="list-row">
                        <button
                          type="button"
                          className="drawer-item"
                          onClick={() => dispatch({ type: 'select-dir', entry })}
                        >
                          <span className="ellipsis drawer-item-name">{entry.name}</span>
                          {entry.vcs_hint === 'git' ? <Tag tone="purple">Git</Tag> : null}
                          {entry.registered_project &&
                          !(dirs.data!.data.partial || dirs.data!.data.warnings.length > 0) ? (
                            <Tag tone="success">已登记</Tag>
                          ) : null}
                          {state.selected?.path === entry.path ? <Tag tone="accent">已选择</Tag> : null}
                        </button>
                        <Button
                          variant="ghost"
                          aria-label={`进入 ${entry.name}`}
                          onClick={() => dispatch({ type: 'enter-dir', path: entry.path })}
                        >
                          进入
                        </Button>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
                <Button
                  variant="primary"
                  disabled={!state.selected || discovery.isPending}
                  title={
                    !state.selected
                      ? '请先选择目录'
                      : discovery.isPending
                        ? '识别在途，请稍候'
                        : undefined
                  }
                  onClick={startProbe}
                >
                  检查并继续
                </Button>
              </div>
            </>
          )
        ) : null}

        {state.step === 'probe' ? (
          <ProbeStep
            state={state}
            probeScope={probeScope}
            probing={discovery.isPending}
            onProbeRetry={startProbe}
            onBack={() => dispatch({ type: 'back-to-dir' })}
            onSubmit={submit}
            onOpenExisting={openExisting}
            onOpenWorkbench={openWorkbench}
            onClose={onClose}
            onSetSlug={(v) => dispatch({ type: 'set-slug', value: v })}
            onSetDisplayName={(v) => dispatch({ type: 'set-display-name', value: v })}
          />
        ) : null}

        {!state.succeeded ? footer : null}
      </section>
    </div>
  )
}

/** 识别失败的原始错误码 → 用户语言 + 明确恢复动作（恢复按钮在调用处统一渲染） */
function describeProbeError(err: ApiError): { title: string; description: string } | null {
  const norm = `${err.code ?? ''} ${err.message}`.replace(/\s+/g, '_')
  if (norm.includes('root_forbidden')) {
    return {
      title: '不能登记这个代码位置本身',
      description:
        '安全规则不允许把代码位置的根目录本身登记进来。请返回选择同一代码位置内直接包含 .git 的仓库目录；如果没有可选目录，请联系管理员调整代码位置配置。',
    }
  }
  if (norm.includes('invalid_locator')) {
    return {
      title: '这个目录不能登记',
      description:
        '所选目录不在允许的代码位置内或已失效。如果这是 Git 项目，请返回选择直接包含 .git 的仓库根目录。',
    }
  }
  return null
}

function ProbeStep({
  state,
  probeScope,
  probing,
  onProbeRetry,
  onBack,
  onSubmit,
  onOpenExisting,
  onOpenWorkbench,
  onClose,
  onSetSlug,
  onSetDisplayName,
}: {
  state: WizardState
  probeScope: CapabilityScope
  probing: boolean
  onProbeRetry: () => void
  onBack: () => void
  onSubmit: () => void
  onOpenExisting: (slug: string) => void
  onOpenWorkbench: () => void
  onClose: () => void
  onSetSlug: (v: string) => void
  onSetDisplayName: (v: string) => void
}) {
  // B3：登记提交消费写 capability（server meta.capabilities 权威；静态 fallback false → disabled）。
  // hook 必须在所有 early return 之前调用。
  const writeCap = useCapability('projectRegistry.write', probeScope)
  if (probing) return <StatusState kind="loading" title="正在识别目录…" />
  if (state.probeError) {
    const err = state.probeError
    const mapped = describeProbeError(err)
    return (
      <>
        <StatusState
          kind={err.retryable ? 'disconnected' : 'error'}
          title={mapped?.title ?? '识别失败'}
          description={mapped?.description ?? err.message}
          children={
            <div className="state-actions">
              {err.retryable ? (
                <button type="button" className="btn btn--primary" onClick={onProbeRetry}>
                  重试
                </button>
              ) : null}
              <button type="button" className="btn btn--ghost" onClick={onBack}>
                返回选择目录
              </button>
            </div>
          }
        />
      </>
    )
  }
  if (state.succeeded) {
    return (
      <section className="panel">
        <h2 className="panel-title">
          <Tag tone="success">添加成功</Tag>
        </h2>
        <div className="kv-grid">
          <span className="kv-key">名称</span>
          <span className="ellipsis">{state.succeeded.displayName}</span>
        </div>
        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button variant="primary" onClick={onOpenWorkbench}>
            继续创建工作空间
          </Button>
          <Button variant="ghost" onClick={onClose}>
            返回列表
          </Button>
        </div>
      </section>
    )
  }
  const probe = state.probe
  if (!probe) return null
  const sub = probeSubstate(probe)
  const degraded = state.probeDegraded
  const slugOk = slugValid(state.slug)
  const nameOk = state.displayName.trim() !== ''
  // I2：无有效指纹 / degraded 探测 / FINGERPRINT_MATCH / 提交在途 → disabled（0 请求）
  const submitReason = degraded
    ? '探测结果不完整（需一次完整识别）'
    : !writeCap.available
      ? (writeCap.reason ?? '暂时无法添加项目，请稍后重试')
      : !nameOk
        ? '请填写项目名称'
        : !slugOk
          ? '标识符格式无效'
          : state.submitting
            ? '正在添加，请稍候'
            : null
  const canSubmit = (sub === 'NEW_GIT' || sub === 'PLAIN_DIR') && submitReason == null

  const submitError = state.submitError

  return (
    <>
      {degraded ? (
        <StatusState
          kind="degraded"
          banner
          title="部分数据源不可用"
          description="暂时无法确认该目录是否已添加；请稍后重试。"
        />
      ) : null}
      {sub === 'NEW_GIT' ? <Tag tone="success">新 Git 项目</Tag> : null}
      {sub === 'PLAIN_DIR' ? <Tag tone="warning">普通目录</Tag> : null}
      {sub === 'ALREADY_REGISTERED' ? <Tag tone="success">已登记</Tag> : null}
      {sub === 'FINGERPRINT_MATCH' ? <Tag tone="success">与已登记项目同源</Tag> : null}
      <p className="list-sub">
        项目目录：{probe.display_path}
        {/* B1：分支只用布尔表达，不渲染 raw branch/upstream 名 */}
        {probe.vcs.kind === 'git' ? ` · Git 仓库（${probe.vcs.branch_present ? '存在分支' : '未命名分支'}）` : ''}
      </p>

      {sub === 'ALREADY_REGISTERED' ? (
        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button variant="primary" onClick={() => onOpenExisting(probe.exact_match!.slug)}>
            打开现有项目
          </Button>
          <Button variant="ghost" onClick={onBack}>
            返回选择目录
          </Button>
        </div>
      ) : sub === 'FINGERPRINT_MATCH' ? (
        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button variant="primary" disabled title="关联能力在后续版本开放">
            关联到已登记项目
          </Button>
          <Button variant="ghost" onClick={onBack}>
            返回选择目录
          </Button>
        </div>
      ) : (
        <>
          {sub === 'PLAIN_DIR' ? (
            <p className="state-reason">该目录不是 Git 仓库；Git 相关能力将在后续版本开放。</p>
          ) : null}
          <div className="kv-grid">
            <label className="kv-key" htmlFor="wizard-display-name">
              项目名称
            </label>
            <input
              id="wizard-display-name"
              className="input"
              aria-label="项目名称"
              value={state.displayName}
              onChange={(e) => onSetDisplayName(e.target.value)}
            />
          </div>
          <details className="wizard-advanced">
            <summary>高级选项</summary>
            <div className="kv-grid">
              <label className="kv-key" htmlFor="wizard-slug">
                标识符
              </label>
              <input
                id="wizard-slug"
                className="input"
                aria-label="标识符"
                value={state.slug}
                onChange={(e) => onSetSlug(e.target.value)}
              />
            </div>
          </details>
          {state.slug !== '' && !slugOk ? (
            <p className="state-reason" role="alert">
              标识符格式无效：小写字母/数字/中划线，首尾非中划线，不含连续中划线，最长 64
            </p>
          ) : null}

          {submitError ? <SubmitErrorNote error={submitError} onBack={onBack} onOpenExisting={() => onOpenExisting(state.slug)} onRetry={onSubmit} /> : null}

          {!submitError || submitError.code === 'project_slug_conflict' ? (
            <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
              <Button
                variant="primary"
                disabled={!canSubmit}
                title={submitReason ?? undefined}
                onClick={onSubmit}
              >
                {state.submitting ? '添加中…' : '确认添加'}
              </Button>
              <Button variant="ghost" onClick={onBack}>
                返回选择目录
              </Button>
            </div>
          ) : null}
        </>
      )}
    </>
  )
}

function SubmitErrorNote({
  error,
  onBack,
  onOpenExisting,
  onRetry,
}: {
  error: ApiError
  onBack: () => void
  onOpenExisting: () => void
  onRetry: () => void
}) {
  if (error.status === 409) {
    if (error.code === 'project_slug_conflict') {
      return <StatusState kind="conflict" banner title="该标识符已被占用" description="请换一个名称后继续。" />
    }
    if (error.code === 'location_already_registered') {
      return (
        <>
          <StatusState kind="conflict" banner title="该目录已经登记" description={error.message} />
          <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
            <Button variant="primary" onClick={onOpenExisting}>
              打开现有项目
            </Button>
            <Button variant="ghost" onClick={onBack}>
              返回选择目录
            </Button>
          </div>
        </>
      )
    }
    if (error.code === 'idempotency_conflict') {
      return (
        <StatusState
          kind="error"
          banner
          title="提交冲突（程序错误）"
          description="提交内容发生冲突；请取消后重开向导再试。"
        />
      )
    }
    // 任何其他 409（含未来 discovery_stale）→ 重新探测路径
    return (
      <>
        <StatusState kind="conflict" banner title="探测后目录状态已变化" description={error.message} />
        <div className="state-actions" style={{ justifyContent: 'flex-start' }}>
          <Button variant="primary" onClick={onBack}>
            重新探测
          </Button>
        </div>
      </>
    )
  }
  // retryable（503/网络）→ 重试复用同一 Idempotency-Key 与 body（T11）
  return (
    <>
      <StatusState kind={error.retryable ? 'disconnected' : 'error'} banner description={error.message} />
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
