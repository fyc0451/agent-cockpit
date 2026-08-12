import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { NAV_ROUTES, type NavRouteMeta } from '../app/routes'
import { useDialog } from '../components/useDialog'
import { useCapability } from '../state/capabilities'

function pathOf(t: NavRouteMeta): string {
  return t.to()
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLElement>(null)
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const close = useCallback(onClose, [onClose])
  const searchCap = useCapability('search.server')
  useDialog(ref, open, close)

  useEffect(() => {
    if (open) {
      setQuery('')
      setActive(0)
    }
  }, [open])

  const items = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return [...NAV_ROUTES]
    return NAV_ROUTES.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        pathOf(t).toLowerCase().includes(q) ||
        (t.keywords ?? '').toLowerCase().includes(q),
    )
  }, [query])

  useEffect(() => {
    setActive((i) => Math.min(i, Math.max(items.length - 1, 0)))
  }, [items.length])

  if (!open) return null

  const go = (t: NavRouteMeta) => {
    close()
    navigate(pathOf(t))
  }

  return (
    <div className="overlay" onClick={close}>
      <section
        ref={ref}
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          className="palette-input"
          aria-label="命令输入框"
          placeholder="输入以筛选页面…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setActive((i) => Math.min(i + 1, items.length - 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setActive((i) => Math.max(i - 1, 0))
            } else if (e.key === 'Enter') {
              e.preventDefault()
              const target = items[active]
              if (target) go(target)
            }
          }}
        />
        <p className="palette-hint">快速前往</p>
        <ul className="palette-list" role="listbox" aria-label="页面导航">
          {items.length === 0 ? (
            <li className="palette-empty">没有匹配的页面</li>
          ) : (
            items.map((t, i) => (
              <li key={t.name}>
                <button
                  type="button"
                  role="option"
                  aria-selected={i === active}
                  className={`palette-item${i === active ? ' palette-item--active' : ''}`}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => go(t)}
                >
                  <span className="ellipsis palette-item-name">{t.name}</span>
                  <code className="palette-item-hash">#{pathOf(t)}</code>
                </button>
              </li>
            ))
          )}
        </ul>
        <p className="palette-foot">
          {searchCap.available ? '' : searchCap.reason ?? '服务端搜索未接通，仅支持页面导航'}
        </p>
      </section>
    </div>
  )
}
