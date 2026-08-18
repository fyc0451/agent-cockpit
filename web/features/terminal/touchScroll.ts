// H5（手机触屏）终端修复，照方移植自 1.0 static/index.html：
//   - enableTermTouchScroll（:4481）：触摸滚动桥 + tap→鼠标点击合成
//   - focusin 软键盘拦截（:4783-4794）
// 背景与已排除的假设见 docs/h5-touch-tap-debugging.md：
//   - xterm 5.5 原生触摸滚动在 mouseTracking/alternate buffer 下不工作；
//   - 真机 tap 落在 link-layer 时 xterm 手势层 preventDefault touchend，
//     浏览器随之抑制兼容鼠标事件，TUI 收不到点击；
//   - 隐藏 textarea（.xterm-helper-textarea）获焦会弹软键盘，输入走专门输入框。

import type { Terminal } from '@xterm/xterm'

/** 1.0 static/index.html:2542 同款判定：触点、粗指针或窄屏任一命中即视为触屏终端。 */
export function isTouchTerminal(): boolean {
  if (typeof navigator !== 'undefined' && navigator.maxTouchPoints > 0) return true
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return (
    window.matchMedia('(any-pointer:coarse)').matches ||
    window.matchMedia('(max-width:560px)').matches
  )
}

interface TapState {
  x: number
  y: number
  t: number
  target: EventTarget | null
  cancelled: boolean
}

/**
 * 触摸滚动桥。xterm 原生处理普通 scrollback；TUI 开启鼠标上报时会跳过内置 touch，
 * alternate buffer 即使未开鼠标上报也没有 touch scrollback。两种情况均把单指拖动
 * 转换成 wheel 交回 xterm：鼠标模式上报给 TUI，alternate 模式转为方向键。
 */
export function enableTermTouchScroll(el: HTMLElement, xterm: Terminal): () => void {
  const sensitivity = 4
  const maxSteps = 48
  let lastY: number | null = null
  let remainder = 0
  let touchActive = false
  let activePointer: number | null = null
  let tapStart: TapState | null = null
  let tapTimer: ReturnType<typeof setTimeout> | null = null
  const cleanups: Array<() => void> = []
  const on = <K extends keyof HTMLElementEventMap>(
    type: K,
    listener: (e: HTMLElementEventMap[K]) => void,
    options?: AddEventListenerOptions,
  ) => {
    el.addEventListener(type, listener as EventListener, options)
    cleanups.push(() => el.removeEventListener(type, listener as EventListener, options))
  }

  const isAlternateBuffer = () => xterm.buffer?.active?.type === 'alternate'
  const needsSyntheticScroll = () =>
    xterm.modes?.mouseTrackingMode !== 'none' || isAlternateBuffer()
  // xterm 5.5 原生只监听 Touch Events；Pointer-only 触屏的三种 buffer/mouse 状态
  // 都必须由本桥转换。提前设置 touch-action，避免浏览器在首个 pointermove 时 cancel。
  const usesPointerEvents = () => typeof PointerEvent === 'function'
  const syncTouchMode = () =>
    xterm.element?.classList.toggle('term-touch-scroll', isAlternateBuffer() || usesPointerEvents())
  syncTouchMode()
  const bufferSub = xterm.buffer?.onBufferChange?.(syncTouchMode)
  if (bufferSub) cleanups.push(() => bufferSub.dispose())

  const finish = () => {
    lastY = null
    remainder = 0
  }
  const begin = (y: number) => {
    lastY = y
    remainder = 0
  }
  const move = (y: number, e: Event, force = false) => {
    if (lastY === null || (!force && !needsSyntheticScroll())) return
    remainder += (lastY - y) * sensitivity
    lastY = y
    const screen = el.querySelector('.xterm-screen')
    const rowHeight = Math.max(
      12,
      (screen?.getBoundingClientRect().height || xterm.rows * 18) / Math.max(1, xterm.rows),
    )
    const lines = remainder > 0 ? Math.floor(remainder / rowHeight) : Math.ceil(remainder / rowHeight)
    if (!lines) return
    // 快滑时一次 move 可能攒很多行；只扣实际派发的部分，余量留到下个事件，不丢距离
    const direction = Math.sign(lines)
    const steps = Math.min(maxSteps, Math.abs(lines))
    remainder -= direction * steps * rowHeight
    if (tapStart) tapStart.cancelled = true // 已实际滚动：本次手势不是 tap，不合成点击
    if (screen && typeof WheelEvent === 'function') {
      window.__h5dbg?.(`bridge wheel dir=${direction} steps=${steps}`)
      for (let i = 0; i < steps; i++) {
        const wheel = new WheelEvent('wheel', {
          deltaY: direction * rowHeight,
          deltaMode: 0,
          bubbles: true,
          cancelable: true,
        })
        screen.dispatchEvent(wheel)
        if (!wheel.defaultPrevented) xterm.scrollLines(direction)
      }
    }
    e.preventDefault()
  }

  on('touchstart', (e) => {
    touchActive = e.touches.length === 1
    activePointer = null
    // H5 调试打点（h5Debug.ts，?debug=1 时存在）：记录滚动桥的起始判定。
    window.__h5dbg?.(
      `bridge ts mouse=${xterm.modes?.mouseTrackingMode ?? '?'} ` +
        `buf=${xterm.buffer?.active?.type ?? '?'} needSynth=${needsSyntheticScroll()}`,
    )
    if (!touchActive || !needsSyntheticScroll()) {
      finish()
      return
    }
    begin(e.touches[0].clientY)
  }, { passive: true })
  on('touchmove', (e) => {
    if (lastY === null || e.touches.length !== 1 || !needsSyntheticScroll()) return
    move(e.touches[0].clientY, e)
  }, { passive: false })
  const finishTouch = () => {
    touchActive = false
    finish()
  }
  on('touchend', finishTouch, { passive: true })
  on('touchcancel', finishTouch, { passive: true })

  // 部分折叠屏展开后按桌面设备派发 Pointer Events，不再提供上述 Touch Events。
  // touchActive 用于浏览器同时派发两套事件时去重，避免一次拖动滚两遍。
  on('pointerdown', (e) => {
    if (e.pointerType !== 'touch' || e.isPrimary === false || touchActive) return
    // 新的 primary pointer 可覆盖极端序列中漏掉 pointerup/cancel 的旧状态。
    activePointer = e.pointerId
    begin(e.clientY)
  }, { passive: true })
  on('pointermove', (e) => {
    if (touchActive || e.pointerType !== 'touch' || e.pointerId !== activePointer) return
    move(e.clientY, e, true)
  }, { passive: false })
  const finishPointer = (e: PointerEvent) => {
    if (e.pointerId !== activePointer) return
    activePointer = null
    finish()
  }
  on('pointerup', finishPointer, { passive: true })
  on('pointercancel', finishPointer, { passive: true })
  on('pointerleave', finishPointer, { passive: true })

  // tap→鼠标点击合成：真机 Chrome 里 tap 落在 xterm link-layer 时，xterm
  // 手势层会在 document 冒泡阶段 preventDefault touchend，浏览器随之不派发
  // 兼容鼠标事件（mousedown/mouseup），开启鼠标追踪的 TUI（如 herdr switch
  // 面板）收不到点击。这里保存 touchend 事件，待 xterm 手势层处理完后检查
  // defaultPrevented——只有它吞掉兼容鼠标事件时才合成，不另设全局守卫。
  const mouseWanted = () =>
    !!xterm.modes?.mouseTrackingMode && xterm.modes.mouseTrackingMode !== 'none'
  on('touchstart', (e) => {
    if (e.touches.length === 1 && mouseWanted()) {
      const t = e.touches[0]
      tapStart = { x: t.clientX, y: t.clientY, t: Date.now(), target: e.target, cancelled: false }
    } else {
      tapStart = null
    }
  }, { passive: true })
  // 二维位移取消（1.0 复审 #1524）：水平拖动不派发 wheel，不会触发滚动取消，
  // 但 xterm 对 END 手势也 preventDefault，会误判成点击。>10px slop 取消，
  // 保留“实际 wheel step 必取消”作第二道保证；不用“任意 move 即取消”，
  // 避免真机手指微抖让正常 tap 失效。
  on('touchmove', (e) => {
    if (!tapStart || e.touches.length !== 1) return
    const t = e.touches[0]
    if (Math.hypot(t.clientX - tapStart.x, t.clientY - tapStart.y) > 10) tapStart.cancelled = true
  }, { passive: true })
  on('touchend', (e) => {
    const tap = tapStart
    tapStart = null
    if (!tap || tap.cancelled || Date.now() - tap.t > 500 || !mouseWanted()) return
    const touchendEvent = e
    tapTimer = setTimeout(() => {
      tapTimer = null
      if (!touchendEvent.defaultPrevented) return // 浏览器会自行派发鼠标事件
      if (!el.isConnected || !el.contains(tap.target as Node | null) || !mouseWanted()) return
      window.__h5dbg?.(`bridge tap synth x=${tap.x} y=${tap.y}`)
      const target = tap.target as Element
      // 不传 view：jsdom 的 MouseEvent 构造器拒绝 view 参数，且 xterm 不读合成事件的 .view
      target.dispatchEvent(
        new MouseEvent('mousedown', {
          bubbles: true,
          cancelable: true,
          button: 0,
          buttons: 1,
          clientX: tap.x,
          clientY: tap.y,
          screenX: tap.x,
          screenY: tap.y,
        }),
      )
      target.dispatchEvent(
        new MouseEvent('mouseup', {
          bubbles: true,
          cancelable: true,
          button: 0,
          buttons: 0,
          clientX: tap.x,
          clientY: tap.y,
          screenX: tap.x,
          screenY: tap.y,
        }),
      )
    }, 30)
  }, { passive: true })

  return () => {
    if (tapTimer) clearTimeout(tapTimer)
    tapTimer = null
    for (const cleanup of cleanups) cleanup()
  }
}

/**
 * H5/触屏：点终端画面或滚动不应弹软键盘。xterm 用隐藏 textarea（.xterm-helper-textarea）
 * 接收键盘，触摸时它获焦会呼出键盘。在 capture 阶段拦截 focusin，阻止该 textarea 获焦。
 * 输入走专门的输入框（点它才弹）。桌面端（isTouchTerminal 为假）完全不挂载，行为零变化。
 */
export function enableTermKeyboardGuard(
  el: HTMLElement,
  isTouch: () => boolean = isTouchTerminal,
): () => void {
  if (!isTouch()) return () => {}
  const onFocusIn = (e: FocusEvent) => {
    const target = e.target
    if (target instanceof HTMLTextAreaElement && target.classList.contains('xterm-helper-textarea')) {
      e.preventDefault()
      e.stopImmediatePropagation()
      // 已获焦则立即收回（避免键盘闪烁）
      if (document.activeElement === target) target.blur()
    }
  }
  el.addEventListener('focusin', onFocusIn, true)
  return () => el.removeEventListener('focusin', onFocusIn, true)
}
