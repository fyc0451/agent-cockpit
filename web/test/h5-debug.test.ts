import { installH5TouchRecorder } from '../features/terminal/h5Debug'

// jsdom 没有 TouchEvent：用普通 Event 挂 touches/changedTouches 属性模拟
// （与 test/term-touch-scroll.test.ts 同款手法）。
function touchEvent(type: string, touches: Array<{ clientX?: number; clientY?: number }>): Event {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'touches', { value: touches })
  Object.defineProperty(event, 'changedTouches', { value: touches })
  return event
}

function overlay(): HTMLElement | null {
  return document.querySelector('[data-testid="h5-debug-overlay"]')
}

function createSurface(): { surface: HTMLElement; inner: HTMLElement } {
  const surface = document.createElement('div')
  surface.className = 'gc-herdr-terminal-surface'
  const inner = document.createElement('div')
  inner.className = 'xterm-rows-inner-padding-extra'
  surface.appendChild(inner)
  document.body.appendChild(surface)
  return { surface, inner }
}

describe('installH5TouchRecorder H5 真机事件记录仪', () => {
  afterEach(() => {
    document.body.innerHTML = ''
    delete window.__h5dbg
    window.history.replaceState({}, '', '/')
  })

  it('无 debug 参数时零副作用', () => {
    const uninstall = installH5TouchRecorder()
    expect(overlay()).toBeNull()
    expect(window.__h5dbg).toBeUndefined()
    const { inner } = createSurface()
    inner.dispatchEvent(touchEvent('touchstart', [{ clientX: 1, clientY: 2 }]))
    expect(overlay()).toBeNull()
    uninstall()
  })

  it('HashRouter 的 /#/?debug=1 与 /#/chat?debug=1 同样打开 overlay', () => {
    window.history.replaceState({}, '', '/#/?debug=1')
    const first = installH5TouchRecorder()
    expect(overlay()).not.toBeNull()
    first()
    window.history.replaceState({}, '', '/#/chat?debug=1')
    installH5TouchRecorder()
    expect(overlay()).not.toBeNull()
    expect(typeof window.__h5dbg).toBe('function')
  })

  it('?debug=1 时创建底部 overlay 并暴露 window.__h5dbg', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const box = overlay()
    expect(box).not.toBeNull()
    expect(box!.style.position).toBe('fixed')
    expect(box!.style.pointerEvents).toBe('none')
    expect(box!.style.zIndex).toBe('99999')
    expect(typeof window.__h5dbg).toBe('function')
  })

  it('记录终端区域内事件：时间戳 + 类型 + class 前 28 字符 + 坐标', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const { inner } = createSurface()
    inner.dispatchEvent(touchEvent('touchstart', [{ clientX: 30.6, clientY: 40.2 }]))
    const cls = 'xterm-rows-inner-padding-extra'.slice(0, 28)
    expect(overlay()!.textContent).toMatch(
      new RegExp(`^\\d+\\.\\d{2} touchstart @${cls} x=31 y=40$`),
    )
  })

  it('终端区域外的事件不记录', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const outside = document.createElement('div')
    document.body.appendChild(outside)
    outside.dispatchEvent(touchEvent('touchstart', [{ clientX: 1, clientY: 2 }]))
    expect(overlay()!.textContent).toBe('')
  })

  it('.terminal-surface 选择器同样命中', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const surface = document.createElement('div')
    surface.className = 'terminal-surface'
    document.body.appendChild(surface)
    surface.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 5, clientY: 6 }))
    expect(overlay()!.textContent).toMatch(/mousedown @terminal-surface x=5 y=6/)
  })

  it('defaultPrevented 的事件带 PD 标记', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const { inner } = createSurface()
    const event = touchEvent('touchend', [{ clientX: 1, clientY: 2 }])
    event.preventDefault()
    inner.dispatchEvent(event)
    expect(overlay()!.textContent).toMatch(/touchend .* PD$/)
  })

  it('focusin 到 TEXTAREA 单独记一行（不限终端区域）', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const textarea = document.createElement('textarea')
    textarea.className = 'xterm-helper-textarea'
    document.body.appendChild(textarea)
    textarea.dispatchEvent(new Event('focusin', { bubbles: true }))
    expect(overlay()!.textContent).toMatch(/focusin textarea/)
  })

  it('最多保留 120 行', () => {
    window.history.replaceState({}, '', '/?debug=1')
    installH5TouchRecorder()
    const { inner } = createSurface()
    for (let i = 0; i < 130; i++) {
      inner.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }
    expect(overlay()!.textContent!.split('\n')).toHaveLength(120)
  })

  it('卸载后移除 overlay、监听与 window.__h5dbg', () => {
    window.history.replaceState({}, '', '/?debug=1')
    const uninstall = installH5TouchRecorder()
    uninstall()
    expect(overlay()).toBeNull()
    expect(window.__h5dbg).toBeUndefined()
    const { inner } = createSurface()
    inner.dispatchEvent(touchEvent('touchstart', [{ clientX: 1, clientY: 2 }]))
    expect(overlay()).toBeNull()
  })
})
