// 新会话向导：选 Leader，轻量拉起 herdr + agent，不走 setup-workspace。

import { useEffect, useMemo, useState } from 'react'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { createChatSession } from '../../api/chatLedger'
import { AGENT_KINDS, agentEmoji, buildLaunchArgs, rootBase, type PermissionMode } from './model'

interface NewSessionWizardProps {
  open: boolean
  workspaceId: string | null
  workdir: string | null // 自动决策出的工作目录；null = 无可访问目录
  existingSessions: string[]
  /** next profile 单会话作用域：非 null 时会话名锁定为该值（server 拒绝其它名字） */
  fixedSessionName?: string | null
  onClose: () => void
  onCreated: (session: string) => void
}

/** 从项目名生成不冲突的默认会话名（herdr session 名仅允许字母数字/_/-） */
function defaultSessionName(project: string, existing: string[]): string {
  const base = `${project.replace(/[^a-zA-Z0-9_-]/g, '-') || 'chat'}-`
  for (let i = 1; i < 100; i++) {
    const candidate = `${base}${i}`
    if (!existing.includes(candidate)) return candidate
  }
  return `${base}${Date.now() % 1000}`
}

export function NewSessionWizard({
  open,
  workspaceId,
  workdir,
  existingSessions,
  fixedSessionName,
  onClose,
  onCreated,
}: NewSessionWizardProps) {
  const [kind, setKind] = useState<string>(AGENT_KINDS[0])
  const [model, setModel] = useState('')
  const [permission, setPermission] = useState<PermissionMode>('ask')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState(0)

  const sessionName = useMemo(
    () =>
      fixedSessionName ??
      defaultSessionName(workdir ? rootBase(workdir) : 'chat', existingSessions),
    [fixedSessionName, workdir, existingSessions],
  )

  useEffect(() => {
    if (!busy) {
      setElapsed(0)
      return
    }
    const timer = window.setInterval(() => setElapsed((n) => n + 1), 1000)
    return () => window.clearInterval(timer)
  }, [busy])

  if (!open) return null

  const close = () => {
    if (busy) return
    setError(null)
    onClose()
  }

  const submit = async () => {
    if (busy || !workdir || !workspaceId) return
    try {
      await requireAuthenticated()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await createChatSession(workspaceId, kind, sessionName, {
        model: model.trim() || undefined,
        args: buildLaunchArgs(kind, model, permission) || undefined,
      })
      onCreated(res.session)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gc-modal-bg" onClick={close}>
      <div className="gc-modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="gc-modal-title">新会话</h3>
        <p className="gc-modal-sub">
          创建一个新的工作现场（workspace），并启动 Leader Agent。
        </p>

        <span className="gc-field-label">Leader Agent</span>
        <div className="gc-agent-grid" role="radiogroup" aria-label="Leader Agent">
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

        {workdir ? (
          <p className="gc-modal-sub">
            工作目录 <b title={workdir}>{rootBase(workdir)}</b> · 会话名 <b>{sessionName}</b>
            {fixedSessionName ? '（由当前实例固定）' : ''}
          </p>
        ) : (
          <div className="gc-modal-error">
            没有工作区目录，暂时不能创建会话。先添加工作区。
          </div>
        )}
        {error && <div className="gc-modal-error">{error}</div>}
        {busy && (
          <div className="gc-busy-hint">
            正在启动 {kind}
            {elapsed >= 5 ? `（已等待 ${elapsed} 秒）` : '…'}
          </div>
        )}

        <div className="gc-modal-actions">
          <button type="button" className="gc-pill-btn" onClick={close} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="gc-pill-btn gc-pill-btn--accent"
            onClick={submit}
            disabled={busy || !workdir}
          >
            {busy ? '创建中…' : '创建会话'}
          </button>
        </div>
      </div>
    </div>
  )
}
