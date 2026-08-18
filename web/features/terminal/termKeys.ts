/** 1.0 static/index.html TERM_KEY_SEQ / applyTermModifiers。 */

export const TERM_KEY_SEQ: Record<string, string> = {
  ArrowUp: '\x1b[A',
  ArrowDown: '\x1b[B',
  ArrowRight: '\x1b[C',
  ArrowLeft: '\x1b[D',
  Enter: '\r',
  Tab: '\t',
  Escape: '\x1b',
  Backspace: '\x7f',
  Delete: '\x1b[3~',
  Home: '\x1b[H',
  End: '\x1b[F',
  PageUp: '\x1b[5~',
  PageDown: '\x1b[6~',
  CtrlC: '\x03',
  CtrlB: '\x02',
  CtrlD: '\x04',
  CtrlL: '\x0c',
  CtrlZ: '\x1a',
  F1: '\x1bOP',
  F2: '\x1bOQ',
  F3: '\x1bOR',
  F4: '\x1bOS',
  F5: '\x1b[15~',
  F6: '\x1b[17~',
  F7: '\x1b[18~',
  F8: '\x1b[19~',
  F9: '\x1b[20~',
  F10: '\x1b[21~',
  F11: '\x1b[23~',
  F12: '\x1b[24~',
}

export interface TermKeySpec {
  name: string
  label: string
  title?: string
  extra?: boolean
  modifier?: 'ctrl' | 'alt' | 'shift'
}

export const TERM_KEYS: TermKeySpec[] = [
  { name: 'ArrowUp', label: '↑', title: '方向键上' },
  { name: 'ArrowDown', label: '↓', title: '方向键下' },
  { name: 'ArrowLeft', label: '←', title: '方向键左' },
  { name: 'ArrowRight', label: '→', title: '方向键右' },
  { name: 'Enter', label: 'Enter' },
  { name: 'Tab', label: 'Tab' },
  { name: 'Escape', label: 'Esc' },
  { name: 'CtrlC', label: 'Ctrl-C' },
  { name: 'CtrlB', label: 'Ctrl-B', title: 'Herdr 前缀键，点后再点方向键切换 pane' },
  { name: 'ctrl', label: 'Ctrl', extra: true, modifier: 'ctrl' },
  { name: 'alt', label: 'Alt', extra: true, modifier: 'alt' },
  { name: 'shift', label: 'Shift', extra: true, modifier: 'shift' },
  { name: 'Home', label: 'Home', extra: true },
  { name: 'End', label: 'End', extra: true },
  { name: 'PageUp', label: 'PgUp', extra: true },
  { name: 'PageDown', label: 'PgDn', extra: true },
  { name: 'Backspace', label: '⌫', extra: true },
  { name: 'Delete', label: 'Delete', extra: true },
  { name: 'CtrlD', label: 'Ctrl-D', extra: true },
  { name: 'CtrlL', label: 'Ctrl-L', extra: true },
  { name: 'CtrlZ', label: 'Ctrl-Z', extra: true },
  { name: 'F1', label: 'F1', extra: true },
  { name: 'F2', label: 'F2', extra: true },
  { name: 'F3', label: 'F3', extra: true },
  { name: 'F4', label: 'F4', extra: true },
  { name: 'F5', label: 'F5', extra: true },
  { name: 'F6', label: 'F6', extra: true },
  { name: 'F7', label: 'F7', extra: true },
  { name: 'F8', label: 'F8', extra: true },
  { name: 'F9', label: 'F9', extra: true },
  { name: 'F10', label: 'F10', extra: true },
  { name: 'F11', label: 'F11', extra: true },
  { name: 'F12', label: 'F12', extra: true },
]

export interface TermModifiers {
  ctrl: boolean
  alt: boolean
  shift: boolean
}

export const EMPTY_MODIFIERS: TermModifiers = { ctrl: false, alt: false, shift: false }

export function applyTermModifiers(
  data: string,
  name: string,
  mods: TermModifiers,
): { seq: string; mods: TermModifiers } {
  if (!mods.ctrl && !mods.alt && !mods.shift) return { seq: data, mods }
  let out = data
  const mod = 1 + (mods.shift ? 1 : 0) + (mods.alt ? 2 : 0) + (mods.ctrl ? 4 : 0)
  const final: Record<string, string> = {
    ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D', Home: 'H', End: 'F',
  }
  const tilde: Record<string, number> = {
    Delete: 3, PageUp: 5, PageDown: 6, F5: 15, F6: 17, F7: 18, F8: 19, F9: 20, F10: 21, F11: 23, F12: 24,
  }
  const functionFinal: Record<string, string> = { F1: 'P', F2: 'Q', F3: 'R', F4: 'S' }
  if (final[name]) out = `\x1b[1;${mod}${final[name]}`
  else if (tilde[name]) out = `\x1b[${tilde[name]};${mod}~`
  else if (functionFinal[name]) out = `\x1b[1;${mod}${functionFinal[name]}`
  else if (out.length === 1) {
    if (mods.shift) {
      const shifted: Record<string, string> = {
        '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
        '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
      }
      out = shifted[out] || (out >= 'a' && out <= 'z' ? out.toUpperCase() : out)
    }
    if (mods.ctrl) {
      const code = out.toUpperCase().charCodeAt(0)
      if (code >= 64 && code <= 95) out = String.fromCharCode(code - 64)
    }
    if (mods.alt) out = `\x1b${out}`
  }
  return { seq: out, mods: EMPTY_MODIFIERS }
}

export function encodeTermKey(name: string, mods: TermModifiers): { seq: string; mods: TermModifiers } | null {
  const raw = TERM_KEY_SEQ[name]
  if (!raw) return null
  return applyTermModifiers(raw, name, mods)
}

/** TUI DECSET 1004：浏览器失焦/回焦不要转发给 PTY。 */
export function isTermFocusReport(data: string): boolean {
  return data === '\x1b[I' || data === '\x1b[O'
}
