import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type ThemePref = 'system' | 'light' | 'dark'
export const THEME_STORAGE_KEY = 'cockpit-v2-theme'
const ORDER: ThemePref[] = ['system', 'light', 'dark']

function prefersDark(): boolean {
  return (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  )
}

export function resolveTheme(pref: ThemePref): 'light' | 'dark' {
  if (pref !== 'system') return pref
  return prefersDark() ? 'dark' : 'light'
}

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    /* ignore */
  }
  return 'system'
}

interface ThemeState {
  pref: ThemePref
  resolved: 'light' | 'dark'
  setPref: (p: ThemePref) => void
  cycle: () => void
}

const ThemeContext = createContext<ThemeState | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(readPref)
  const resolved = resolveTheme(pref)

  useEffect(() => {
    const resolved = resolveTheme(pref)
    document.documentElement.dataset.theme = resolved
    // dsw 令牌体系（features/shell/dsw.css）挂在 body[data-ds-dark-theme] 上，
    // 与 html[data-theme] 同步驱动，native 控件跟随 color-scheme。
    document.body.toggleAttribute('data-ds-dark-theme', resolved === 'dark')
    document.documentElement.style.colorScheme = resolved
    try {
      localStorage.setItem(THEME_STORAGE_KEY, pref)
    } catch {
      /* ignore */
    }
    if (pref !== 'system' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => {
      const next = mq.matches ? 'dark' : 'light'
      document.documentElement.dataset.theme = next
      document.body.toggleAttribute('data-ds-dark-theme', next === 'dark')
      document.documentElement.style.colorScheme = next
    }
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [pref])

  const setPref = useCallback((p: ThemePref) => setPrefState(p), [])
  const cycle = useCallback(() => {
    setPrefState((cur) => ORDER[(ORDER.indexOf(cur) + 1) % ORDER.length])
  }, [])

  const value = useMemo<ThemeState>(() => ({ pref, resolved, setPref, cycle }), [pref, resolved, setPref, cycle])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider')
  return ctx
}
