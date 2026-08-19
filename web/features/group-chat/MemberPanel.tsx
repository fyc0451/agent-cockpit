// 右栏：群成员列表（leader 置顶 + 我）+ 添加成员（session 内新开 tab 启动 agent）。

import { useEffect, useRef, useState } from 'react'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { closePane, restartPane, startAgent } from '../../api/legacyHerdr'
import { AgentIcon } from './AgentIcon'
import {
  AGENT_KINDS,
  agentEmoji,
  avatarColor,
  buildLaunchArgs,
  statusMeta,
  unreadCountLabel,
  type ChatMember,
  type PermissionMode,
} from './model'

interface MemberPanelProps {
  members: ChatMember[]
  session: string | null
  workdir: string | null // 新成员的工作目录（leader 的 cwd 优先）
  open: boolean // 窄屏抽屉态
  onMention: (m: ChatMember) => void
  onFilter: (m: ChatMember) => void // hover「只看 TA」
  onInteract: (m: ChatMember) => void
  onOpenTerminal: () => void
  onChanged: () => void // 成员变更后刷新 snapshot
  externalAddSignal?: number // 输入卡片「＋」快捷入口：值变化即打开添加成员弹窗
}

function AddMemberModal({
  session,
  workdir,
  onClose,
  onAdded,
}: {
  session: string
  workdir: string | null
  onClose: () => void
  onAdded: () => void
}) {
  const [kind, setKind] = useState<string>(AGENT_KINDS[0])
  const [name, setName] = useState('')
  const [model, setModel] = useState('')
  const [permission, setPermission] = useState<PermissionMode>('ask')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSubmit = !busy && !!workdir

  const submit = async () => {
    if (!canSubmit || !workdir) return
    try {
      await requireAuthenticated()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await startAgent({
        session,
        workdir,
        agent: kind,
        name: name.trim() || undefined,
        layout: 'tab',
        workspace: 'shared',
        args: buildLaunchArgs(kind, model, permission),
      })
      if (res.error) {
        setError(res.error)
        return
      }
      onAdded()
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gc-modal-bg" onClick={busy ? undefined : onClose}>
      <div className="gc-modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="gc-modal-title">添加成员</h3>
        <p className="gc-modal-sub">在会话「{session}」中新开一个 tab 并启动 Agent。</p>

        <span className="gc-field-label">Agent 类型</span>
        <div className="gc-agent-grid" role="radiogroup" aria-label="Agent 类型">
          {AGENT_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={kind === k}
              className={`gc-agent-card${kind === k ? ' is-selected' : ''}`}
              onClick={() => setKind(k)}
              disabled={busy}
            >
              <span className="gc-agent-emoji">{agentEmoji(k)}</span>
              <span>{k}</span>
            </button>
          ))}
        </div>

        <span className="gc-field-label">显示名（可选）</span>
        <input
          className="gc-input"
          value={name}
          placeholder={kind}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
        />

        <span className="gc-field-label">模型（可选）</span>
        <input
          className="gc-input"
          value={model}
          placeholder={kind === 'kimi' ? 'kimi-code/k3' : '启动时 -m'}
          disabled={busy}
          onChange={(e) => setModel(e.target.value)}
        />

        {kind === 'kimi' && (
          <>
            <span className="gc-field-label">权限</span>
            <div className="gc-perm-row">
              {([
                ['ask', '每次询问'],
                ['yolo', '自动批准工具'],
                ['auto', '完全自主'],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={`gc-perm-chip${permission === value ? ' is-selected' : ''}`}
                  disabled={busy}
                  onClick={() => setPermission(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </>
        )}

        {!workdir && <div className="gc-modal-error">无法确定工作目录（会话还没有 leader）。</div>}
        {error && <div className="gc-modal-error">{error}</div>}
        {busy && <div className="gc-busy-hint">正在启动 Agent，约 30 秒，请稍候…</div>}

        <div className="gc-modal-actions">
          <button type="button" className="gc-pill-btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="gc-pill-btn gc-pill-btn--accent"
            onClick={submit}
            disabled={!canSubmit}
          >
            {busy ? '启动中…' : '添加'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function MemberPanel({
  members,
  session,
  workdir,
  open,
  onMention,
  onFilter,
  onInteract,
  onOpenTerminal,
  onChanged,
  externalAddSignal,
}: MemberPanelProps) {
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<{ paneId: string; kind: 'close' | 'restart' } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const lastSignalRef = useRef(externalAddSignal)
  useEffect(() => {
    if (externalAddSignal !== undefined && externalAddSignal !== lastSignalRef.current) {
      lastSignalRef.current = externalAddSignal
      if (session) setAdding(true)
    }
  }, [externalAddSignal, session])

  // 排序：Leader 第一 → 我（Boss）第二 → 其他 agent
  const leaderAgents = members.filter((m) => m.isLeader)
  const restAgents = members.filter((m) => !m.isLeader)

  const renderAgent = (m: (typeof members)[number]) => {
    const meta = statusMeta(m.status)
    const unread = unreadCountLabel(m.unread)
    return (
      <div key={m.paneId} className="gc-member-row">
        <button
          type="button"
          className="gc-member"
          title={unread ? `@${m.name} · ${meta.label} · ${m.unread} 条未读` : `@${m.name} · ${meta.label}`}
          onClick={() => onMention(m)}
        >
          <span
            className={`gc-member-avatar gc-member-avatar--${m.kind.toLowerCase()}`}
            style={{ background: avatarColor(m.kind) }}
            aria-hidden
          >
            <AgentIcon kind={m.kind} />
            {unread && <span className="gc-unread-badge gc-unread-badge--avatar">{unread}</span>}
          </span>
          <span className="gc-member-main">
            <span className="gc-member-name">
              {m.name}
              {m.isLeader && <span className="gc-leader-badge">Leader</span>}
            </span>
            <span className="gc-member-sub">
              <span className={`gc-dot ${meta.dot}`} aria-hidden />
              {m.kind} · {meta.label}
              {unread && ` · ${unread} 未读`}
            </span>
          </span>
        </button>
        <span className="gc-member-ops">
          <button
            type="button"
            className="gc-member-op"
            title={m.status === 'blocked' ? `处理 ${m.name}` : `打开 ${m.name} 的终端`}
            onClick={() => {
              if (m.status === 'blocked') onInteract(m)
              else onOpenTerminal()
            }}
          >
            {m.status === 'blocked' ? '处理' : '终端'}
          </button>
          <button
            type="button"
            className="gc-member-op"
            title={`只看 ${m.name}`}
            onClick={() => onFilter(m)}
          >
            只看TA
          </button>
          <button
            type="button"
            className="gc-member-op"
            title={`重启 ${m.name}`}
            aria-label={`重启 ${m.name}`}
            disabled={!session || busy !== null}
            onClick={() => {
              if (!session || busy) return
              if (!window.confirm(`重启成员 ${m.name}？当前任务会被打断。`)) return
              setBusy({ paneId: m.paneId, kind: 'restart' })
              setActionError(null)
              void restartPane(session, m.paneId)
                .then(() => {
                  onChanged()
                })
                .catch((e) => {
                  setActionError(e instanceof ApiError ? e.message : String(e))
                })
                .finally(() => {
                  setBusy((current) => (
                    current?.paneId === m.paneId && current.kind === 'restart' ? null : current
                  ))
                })
            }}
          >
            {busy?.paneId === m.paneId && busy.kind === 'restart' ? '重启中' : '重启'}
          </button>
          <button
            type="button"
            className="gc-member-op gc-member-op--danger"
            title={`关闭 ${m.name}`}
            aria-label={`关闭 ${m.name}`}
            disabled={!session || busy !== null}
            onClick={() => {
              if (!session || busy) return
              if (!window.confirm(`关闭成员 ${m.name}？终端会一起关掉。`)) return
              setBusy({ paneId: m.paneId, kind: 'close' })
              setActionError(null)
              void closePane(session, m.paneId)
                .then(() => {
                  onChanged()
                })
                .catch((e) => {
                  setActionError(e instanceof ApiError ? e.message : String(e))
                })
                .finally(() => {
                  setBusy((current) => (
                    current?.paneId === m.paneId && current.kind === 'close' ? null : current
                  ))
                })
            }}
          >
            {busy?.paneId === m.paneId && busy.kind === 'close' ? '关闭中' : '关闭'}
          </button>
        </span>
      </div>
    )
  }

  return (
    <aside className={`gc-members${open ? ' is-open' : ''}`} aria-label="群成员">
      <div className="gc-members-head">
        <span>群成员</span>
        <span className="gc-members-count">· {members.length + 1}</span>
        <button
          type="button"
          className="gc-open-terminal"
          title={session ? `打开 ${session} 的 Herdr 终端` : '请先选择会话'}
          aria-label="打开 Herdr 终端"
          disabled={!session}
          onClick={onOpenTerminal}
        >
          <span aria-hidden>&gt;_</span>
        </button>
        <button
          type="button"
          className="gc-add-member"
          title="添加成员"
          disabled={!session}
          onClick={() => setAdding(true)}
        >
          ＋
        </button>
      </div>
      <div className="gc-member-list">
        {leaderAgents.map(renderAgent)}
        {/* 我（Boss）：不是 pane，只作身份展示；点击不插 @，不可发送给自己 */}
        <div className="gc-member gc-member--me" title="Boss：下任务、看回复、加人">
          <span className="gc-member-avatar gc-member-avatar--me" aria-hidden>
            🙂
          </span>
          <span className="gc-member-main">
            <span className="gc-member-name">
              我<span className="gc-boss-badge">Boss</span>
            </span>
            <span className="gc-member-sub">人类</span>
          </span>
        </div>
        {restAgents.map(renderAgent)}
        {actionError && <div className="gc-modal-error">{actionError}</div>}
      </div>
      {adding && session && (
        <AddMemberModal
          session={session}
          workdir={workdir}
          onClose={() => setAdding(false)}
          onAdded={onChanged}
        />
      )}
    </aside>
  )
}
