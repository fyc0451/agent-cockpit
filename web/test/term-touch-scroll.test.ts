import type { Terminal } from '@xterm/xterm'
import {
  enableTermKeyboardGuard,
  enableTermTouchScroll,
  isTouchTerminal,
} from '../features/terminal/touchScroll'

// jsdom 没有 TouchEvent：用普通 Event 挂 touches/changedTouches 属性模拟。
function touchEvent(type: string, touches: Array<{ clientX?: number; clientY?: number }>): Event {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'touches', { value: touches })
  Object.defineProperty(event, 'changedTouches', { value: touches })
  return event
}

interface XtermStub {
  rows: number
  modes: { mouseTrackingMode: string }
  buffer: { active: { type: string }; onBufferChange: (cb: () => void) => { dispose: () => void } }
  element: HTMLElement
  screen: HTMLElement
  scrollLines: ReturnType<typeof vi.fn>
  bufferChangeCb: (() => void) | null
}

function createXtermStub(): XtermStub {
  const element = document.createElement('div')
  element.className = 'xterm'
  const screen = document.createElement('div')
  screen.className = 'xterm-screen'
  element.appendChild(screen)
  const stub: XtermStub = {
    rows: 24,
    modes: { mouseTrackingMode: 'any' },
    buffer: {
      active: { type: 'normal' },
      onBufferChange(cb) {
        stub.bufferChangeCb = cb
        return { dispose() {} }
      },
    },
    element,
    screen,
    scrollLines: vi.fn(),
    bufferChangeCb: null,
  }
  return stub
}

function createHost(): { el: HTMLElement; xterm: XtermStub } {
  const el = document.createElement('div')
  const xterm = createXtermStub()
  el.appendChild(xterm.element)
  document.body.appendChild(el)
  return { el, xterm }
}

describe('enableTermTouchScroll 触摸滚动桥', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('mouseTracking 下单指拖动按行换算派发 WheelEvent 并 scrollLines', () => {
    const { el, xterm } = createHost()
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const wheels: WheelEvent[] = []
    xterm.screen.addEventListener('wheel', (e) => wheels.push(e as WheelEvent))

    // rowHeight = 24*18/24 = 18；拖动 100px * 敏感度 4 = 400 → 22 行
    el.dispatchEvent(touchEvent('touchstart', [{ clientY: 200 }]))
    el.dispatchEvent(touchEvent('touchmove', [{ clientY: 100 }]))
    el.dispatchEvent(touchEvent('touchend', []))

    expect(wheels).toHaveLength(22)
    expect(wheels[0].deltaY).toBe(18)
    expect(xterm.scrollLines).toHaveBeenCalledTimes(22)
    expect(xterm.scrollLines).toHaveBeenCalledWith(1)
  })

  it('普通 buffer 且未开 mouseTracking 时不接管触摸滚动', () => {
    const { el, xterm } = createHost()
    xterm.modes.mouseTrackingMode = 'none'
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const wheels: WheelEvent[] = []
    xterm.screen.addEventListener('wheel', (e) => wheels.push(e as WheelEvent))

    el.dispatchEvent(touchEvent('touchstart', [{ clientY: 200 }]))
    el.dispatchEvent(touchEvent('touchmove', [{ clientY: 100 }]))
    el.dispatchEvent(touchEvent('touchend', []))

    expect(wheels).toHaveLength(0)
    expect(xterm.scrollLines).not.toHaveBeenCalled()
  })

  it('alternate buffer 即使未开 mouseTracking 也合成 wheel', () => {
    const { el, xterm } = createHost()
    xterm.modes.mouseTrackingMode = 'none'
    xterm.buffer.active.type = 'alternate'
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const wheels: WheelEvent[] = []
    xterm.screen.addEventListener('wheel', (e) => wheels.push(e as WheelEvent))

    el.dispatchEvent(touchEvent('touchstart', [{ clientY: 200 }]))
    el.dispatchEvent(touchEvent('touchmove', [{ clientY: 100 }]))
    el.dispatchEvent(touchEvent('touchend', []))

    expect(wheels.length).toBeGreaterThan(0)
    expect(xterm.scrollLines).toHaveBeenCalled()
  })

  it('不足一行的位移累积到后续事件，不丢距离', () => {
    const { el, xterm } = createHost()
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const wheels: WheelEvent[] = []
    xterm.screen.addEventListener('wheel', (e) => wheels.push(e as WheelEvent))

    el.dispatchEvent(touchEvent('touchstart', [{ clientY: 200 }]))
    el.dispatchEvent(touchEvent('touchmove', [{ clientY: 198 }])) // 2*4=8 < 18，不派发
    expect(wheels).toHaveLength(0)
    el.dispatchEvent(touchEvent('touchmove', [{ clientY: 195 }])) // 再 3*4=12，累计 20 → 1 行
    expect(wheels).toHaveLength(1)
    expect(xterm.scrollLines).toHaveBeenCalledTimes(1)
    el.dispatchEvent(touchEvent('touchend', []))
  })

  it('alternate buffer 时给 xterm.element 加 term-touch-scroll 类', () => {
    const { el, xterm } = createHost()
    const detach = enableTermTouchScroll(el, xterm as unknown as Terminal)
    expect(xterm.element.classList.contains('term-touch-scroll')).toBe(false)
    xterm.buffer.active.type = 'alternate'
    xterm.bufferChangeCb?.()
    expect(xterm.element.classList.contains('term-touch-scroll')).toBe(true)
    detach()
  })
})

describe('enableTermTouchScroll tap→点击合成', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    document.body.innerHTML = ''
  })

  function recordMouse(target: HTMLElement): string[] {
    const seen: string[] = []
    target.addEventListener('mousedown', () => seen.push('mousedown'))
    target.addEventListener('mouseup', () => seen.push('mouseup'))
    return seen
  }

  it('touchend 被 xterm 手势层 preventDefault 时合成 mousedown/mouseup', () => {
    const { el, xterm } = createHost()
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const seen = recordMouse(xterm.screen)
    // 模拟 xterm 手势层吞掉兼容鼠标事件
    el.addEventListener('touchend', (e) => e.preventDefault())

    xterm.screen.dispatchEvent(touchEvent('touchstart', [{ clientX: 10, clientY: 10 }]))
    xterm.screen.dispatchEvent(touchEvent('touchend', []))
    vi.advanceTimersByTime(50)

    expect(seen).toEqual(['mousedown', 'mouseup'])
  })

  it('touchend 未被 preventDefault（浏览器会自行派发鼠标事件）时不合成', () => {
    const { el, xterm } = createHost()
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const seen = recordMouse(xterm.screen)

    el.dispatchEvent(touchEvent('touchstart', [{ clientX: 10, clientY: 10 }]))
    xterm.screen.dispatchEvent(touchEvent('touchend', []))
    vi.advanceTimersByTime(50)

    expect(seen).toEqual([])
  })

  it('移动超过 10px slop 视为滚动，不合成点击', () => {
    const { el, xterm } = createHost()
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const seen = recordMouse(xterm.screen)
    el.addEventListener('touchend', (e) => e.preventDefault())

    el.dispatchEvent(touchEvent('touchstart', [{ clientX: 10, clientY: 10 }]))
    el.dispatchEvent(touchEvent('touchmove', [{ clientX: 10, clientY: 30 }]))
    el.dispatchEvent(touchEvent('touchend', []))
    vi.advanceTimersByTime(50)

    expect(seen).toEqual([])
  })

  it('未开 mouseTracking 时不记录 tap，不合成点击', () => {
    const { el, xterm } = createHost()
    xterm.modes.mouseTrackingMode = 'none'
    enableTermTouchScroll(el, xterm as unknown as Terminal)
    const seen = recordMouse(xterm.screen)
    el.addEventListener('touchend', (e) => e.preventDefault())

    el.dispatchEvent(touchEvent('touchstart', [{ clientX: 10, clientY: 10 }]))
    xterm.screen.dispatchEvent(touchEvent('touchend', []))
    vi.advanceTimersByTime(50)

    expect(seen).toEqual([])
  })
})

describe('enableTermKeyboardGuard 软键盘拦截', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  function createGuardHost(): { el: HTMLElement; textarea: HTMLTextAreaElement } {
    const el = document.createElement('div')
    const textarea = document.createElement('textarea')
    textarea.className = 'xterm-helper-textarea'
    el.appendChild(textarea)
    document.body.appendChild(el)
    return { el, textarea }
  }

  it('触屏设备：capture 阶段拦截 focusin，隐藏 textarea 无法获焦', () => {
    const { el, textarea } = createGuardHost()
    enableTermKeyboardGuard(el, () => true)

    const event = new FocusEvent('focusin', { bubbles: true, cancelable: true })
    textarea.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)

    textarea.focus()
    expect(document.activeElement).not.toBe(textarea)
  })

  it('触屏设备：非 xterm-helper-textarea 的输入框不受影响', () => {
    const { el, textarea } = createGuardHost()
    textarea.className = 'chat-input'
    enableTermKeyboardGuard(el, () => true)

    textarea.focus()
    expect(document.activeElement).toBe(textarea)
  })

  it('非触屏设备：不挂载拦截，textarea 正常获焦', () => {
    const { el, textarea } = createGuardHost()
    enableTermKeyboardGuard(el, () => false)

    const event = new FocusEvent('focusin', { bubbles: true, cancelable: true })
    textarea.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)

    textarea.focus()
    expect(document.activeElement).toBe(textarea)
  })
})

describe('isTouchTerminal', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('有触点即为触屏终端', () => {
    vi.stubGlobal('navigator', { ...navigator, maxTouchPoints: 1 })
    expect(isTouchTerminal()).toBe(true)
  })

  it('无触点且指针不粗、屏不窄时为桌面（jsdom 默认环境）', () => {
    expect(isTouchTerminal()).toBe(false)
  })
})
