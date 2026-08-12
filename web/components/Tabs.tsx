import { useEffect, useRef, useState } from 'react'

export interface TabDef {
  key: string
  label: string
}

export function tabId(key: string): string {
  return `tab-${key}`
}

export function tabPanelId(key: string): string {
  return `panel-${key}`
}

/**
 * 通用 tablist：roving tabindex（active tab tabindex=0，其余 -1）+
 * ArrowLeft/ArrowRight 循环、Home/End 跳首尾。
 * 激活策略（注释说明）：激活跟随焦点（automatic activation）——方向键移动即切换 active，
 * 无需再按 Enter/Space；click 直接激活不额外移焦。
 */
export function Tabs({
  tabs,
  active,
  onChange,
  ariaLabel,
}: {
  tabs: readonly TabDef[]
  active: string
  onChange: (key: string) => void
  ariaLabel: string
}) {
  const refs = useRef(new Map<string, HTMLButtonElement>())
  const [pendingFocus, setPendingFocus] = useState<string | null>(null)

  useEffect(() => {
    if (pendingFocus) {
      refs.current.get(pendingFocus)?.focus()
      setPendingFocus(null)
    }
  }, [pendingFocus, active])

  const activate = (key: string, moveFocus: boolean) => {
    onChange(key)
    if (moveFocus) setPendingFocus(key)
  }

  return (
    <div className="tabs" role="tablist" aria-label={ariaLabel}>
      {tabs.map((t, i) => (
        <button
          key={t.key}
          ref={(el) => {
            if (el) refs.current.set(t.key, el)
            else refs.current.delete(t.key)
          }}
          type="button"
          role="tab"
          id={tabId(t.key)}
          aria-selected={active === t.key}
          aria-controls={tabPanelId(t.key)}
          tabIndex={active === t.key ? 0 : -1}
          className={`tab${active === t.key ? ' tab--active' : ''}`}
          onClick={() => activate(t.key, false)}
          onKeyDown={(e) => {
            let next: number | null = null
            if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
            else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
            else if (e.key === 'Home') next = 0
            else if (e.key === 'End') next = tabs.length - 1
            if (next != null) {
              e.preventDefault()
              activate(tabs[next].key, true)
            }
          }}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
