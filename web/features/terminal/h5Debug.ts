// H5 真机触控事件记录仪（临时调试设施，?debug=1 门控，用完即撤）。
// 用途：定位「手机终端不能滚动」类问题的真机事件序列。方案照 1.0
// docs/h5-touch-tap-debugging.md 附录的记录仪移植，目标选择器换成 3.0 的
// .gc-herdr-terminal-surface / .terminal-surface。

declare global {
  interface Window {
    /** ?debug=1 时由 installH5TouchRecorder 挂载的调试日志函数。 */
    __h5dbg?: (msg: string) => void
  }
}

const EVENT_TYPES = [
  'touchstart',
  'touchmove',
  'touchend',
  'touchcancel',
  'pointerdown',
  'pointermove',
  'pointerup',
  'pointercancel',
  'mousedown',
  'mouseup',
  'click',
  'wheel',
  'focusin',
] as const

const TERMINAL_SELECTOR = '.gc-herdr-terminal-surface, .terminal-surface'
const MAX_LINES = 120

/** HashRouter 下 `/?debug=1` 和 `/#/?debug=1`、`/#/chat?debug=1` 都算打开。 */
export function h5DebugEnabled(): boolean {
  return /(?:[?&#])debug=1(?:&|$)/.test(
    `${window.location.search}${window.location.hash}`,
  )
}

/**
 * 挂载真机事件记录仪。仅当 URL 含 debug=1 时生效，否则零副作用。
 * 返回卸载函数（移除 overlay、监听与 window.__h5dbg）。
 */
export function installH5TouchRecorder(): () => void {
  if (!h5DebugEnabled()) return () => {}

  const box = document.createElement('div')
  box.dataset.testid = 'h5-debug-overlay'
  box.style.cssText =
    'position:fixed;left:0;right:0;bottom:0;max-height:38vh;overflow:auto;' +
    'background:rgba(0,0,0,.88);color:#7fff9a;font:10px/1.35 monospace;' +
    'z-index:99999;padding:4px 6px;white-space:pre-wrap;pointer-events:none'
  document.body.appendChild(box)

  const lines: string[] = []
  const log = (msg: string) => {
    const t = (performance.now() / 1000).toFixed(2)
    lines.push(`${t} ${msg}`)
    if (lines.length > MAX_LINES) lines.shift()
    box.textContent = lines.join('\n')
    box.scrollTop = box.scrollHeight
  }
  window.__h5dbg = log

  const onEvent = (e: Event) => {
    const target = e.target as Element | null
    if (!target || typeof target.closest !== 'function') return
    if (!target.closest(TERMINAL_SELECTOR)) return
    // focusin 到 TEXTAREA 由下方专行记录（软键盘相关），这里跳过避免重复。
    if (e.type === 'focusin' && target.tagName === 'TEXTAREA') return
    const touch = (e as TouchEvent).changedTouches?.[0]
    const pt = touch ?? (e as PointerEvent)
    const cls = (target.getAttribute('class') || target.tagName || '').toString().slice(0, 28)
    log(
      `${e.type} @${cls} x=${Math.round(pt.clientX || 0)} y=${Math.round(pt.clientY || 0)}` +
        (e.defaultPrevented ? ' PD' : ''),
    )
  }
  for (const type of EVENT_TYPES) {
    document.addEventListener(type, onEvent, { capture: true, passive: true })
  }
  // 软键盘相关：TEXTAREA 获焦单独记一行，不限制在终端区域内（xterm 的隐藏
  // textarea 获焦会弹软键盘，是滚动/点击问题的常见旁证）。
  const onFocusIn = (e: Event) => {
    if ((e.target as Element | null)?.tagName === 'TEXTAREA') log('focusin textarea')
  }
  document.addEventListener('focusin', onFocusIn, { capture: true, passive: true })

  return () => {
    for (const type of EVENT_TYPES) {
      document.removeEventListener(type, onEvent, { capture: true })
    }
    document.removeEventListener('focusin', onFocusIn, { capture: true })
    box.remove()
    if (window.__h5dbg === log) delete window.__h5dbg
  }
}
