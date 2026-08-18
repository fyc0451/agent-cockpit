// 添加工作区 = 选一个工作目录。浏览逻辑对齐 3080 DirectoryBrowser：
// 打开从 Home 列一层；点进去 / 面包屑往上；只列目录；隐藏项默认不显示。

import { useEffect, useMemo, useState } from 'react'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { createChatWorkspace } from '../../api/chatLedger'
import { browsePickerDir, type BrowseListing } from '../../api/legacyFiles'

interface AddWorkspaceModalProps {
  roots: string[]
  onClose: () => void
  onAdded: () => void
}

function displayCrumbs(listing: BrowseListing): Array<{ name: string; path: string }> {
  const homeIdx = listing.crumbs.findIndex((c) => c.path === listing.home)
  if (homeIdx === -1) return listing.crumbs
  return [{ name: 'Home', path: listing.home }, ...listing.crumbs.slice(homeIdx + 1)]
}

export function AddWorkspaceModal({ roots, onClose, onAdded }: AddWorkspaceModalProps) {
  const [listing, setListing] = useState<BrowseListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [showHidden, setShowHidden] = useState(false)
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const go = (target?: string) => {
    setLoading(true)
    setError(null)
    setSelected(null)
    browsePickerDir(target)
      .then((next) => {
        setListing(next)
        setPath(next.path)
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e))
      })
      .finally(() => {
        setLoading(false)
      })
  }

  useEffect(() => {
    go()
  }, [])

  const rootSet = useMemo(() => new Set(roots.map((r) => r.replace(/\/+$/, ''))), [roots])
  const visible = useMemo(() => {
    if (!listing) return []
    return listing.entries.filter((e) => showHidden || !e.hidden)
  }, [listing, showHidden])

  const target = (selected ?? listing?.path ?? '').replace(/\/+$/, '') || '/'
  const already = rootSet.has(target)

  const add = async (raw: string) => {
    const p = raw.trim()
    if (!p || busy) return
    try {
      await requireAuthenticated()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await createChatWorkspace(p)
      onAdded()
      onClose()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  const crumbs = listing ? displayCrumbs(listing) : []

  return (
    <div className="gc-modal-bg" onClick={busy ? undefined : onClose}>
      <div className="gc-modal gc-modal--picker" onClick={(e) => e.stopPropagation()}>
        <h3 className="gc-modal-title">添加工作区</h3>
        <p className="gc-modal-sub">从 Home 往下选一个目录。不能把 / 或整个 Home 加成工作区。</p>

        <div className="gc-crumbs" aria-label="当前路径">
          {crumbs.map((c) => (
            <button
              key={c.path}
              type="button"
              className="gc-crumb"
              disabled={busy || loading || c.path === listing?.path}
              onClick={() => go(c.path)}
              title={c.path}
            >
              {c.name}
            </button>
          ))}
        </div>

        <div className="gc-picker-list" role="listbox" aria-label="目录">
          {loading && <div className="gc-side-empty">正在读取目录…</div>}
          {!loading && visible.length === 0 && (
            <div className="gc-side-empty">这一层没有可进入的目录</div>
          )}
          {!loading &&
            visible.map((e) => {
              const added = rootSet.has(e.path.replace(/\/+$/, ''))
              return (
                <div key={e.path} className="gc-picker-row">
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected === e.path}
                    className={`gc-project-chip${selected === e.path ? ' is-selected' : ''}`}
                    disabled={busy}
                    title={added ? `${e.path}（已是工作区）` : e.path}
                    onClick={() => setSelected(e.path)}
                    onDoubleClick={() => go(e.path)}
                  >
                    <span className="gc-project-name">
                      📂 {e.name}
                      {added && <span className="gc-mention-kind">（已添加）</span>}
                    </span>
                    <span className="gc-project-path">{e.path}</span>
                  </button>
                  <button
                    type="button"
                    className="gc-pill-btn"
                    disabled={busy}
                    onClick={() => go(e.path)}
                  >
                    进入
                  </button>
                </div>
              )
            })}
        </div>
        {listing?.truncated && (
          <p className="gc-modal-sub">这一层目录很多，只显示了前 1000 个。</p>
        )}

        <label className="gc-picker-hidden">
          <input
            type="checkbox"
            checked={showHidden}
            disabled={busy}
            onChange={(e) => setShowHidden(e.target.checked)}
          />
          显示隐藏目录
        </label>

        <span className="gc-field-label">或输入路径</span>
        <div className="gc-addpath-row">
          <input
            className="gc-input"
            value={path}
            placeholder="~/github/my-project 或 /home/…"
            disabled={busy}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') go(path)
            }}
          />
          <button
            type="button"
            className="gc-pill-btn"
            disabled={busy || !path.trim()}
            onClick={() => go(path)}
          >
            转到
          </button>
        </div>

        {error && <div className="gc-modal-error">{error}</div>}

        <div className="gc-modal-actions">
          <button type="button" className="gc-pill-btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="gc-pill-btn gc-pill-btn--accent"
            disabled={busy || !target || already}
            title={already ? '已是工作区' : target}
            onClick={() => add(target)}
          >
            添加此目录
          </button>
        </div>
      </div>
    </div>
  )
}
