/** 1.0 终端字体：本机 localStorage，10–24，改完立刻 fit。 */

export const TERM_FONT_MIN = 10
export const TERM_FONT_MAX = 24
export const TERM_FONT_DEFAULT = 13
export const TERM_FONT_STORAGE_KEY = 'term-font-size'

export function clampTermFontSize(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(parsed)) return TERM_FONT_DEFAULT
  return Math.min(TERM_FONT_MAX, Math.max(TERM_FONT_MIN, Math.round(parsed)))
}

export function loadTermFontSize(): number {
  try {
    return clampTermFontSize(window.localStorage.getItem(TERM_FONT_STORAGE_KEY))
  } catch {
    return TERM_FONT_DEFAULT
  }
}

export function saveTermFontSize(value: number): number {
  const size = clampTermFontSize(value)
  try {
    window.localStorage.setItem(TERM_FONT_STORAGE_KEY, String(size))
  } catch {
    /* 隐私模式写不了就只改当次 */
  }
  return size
}

export function stepTermFontSize(current: number, delta: number): number {
  return clampTermFontSize(current + delta)
}
