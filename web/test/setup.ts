import '@testing-library/jest-dom/vitest'

// jsdom 未实现 canvas（缺 canvas 原生包）：xterm 探测 webgl/2d 时会向 stderr 打
// "Not implemented: HTMLCanvasElement.prototype.getContext"。mock 为返回 null，
// xterm 回退 DOM renderer——测试只关心终端外壳挂载，不断言渲染后端。
HTMLCanvasElement.prototype.getContext = (() =>
  null) as unknown as typeof HTMLCanvasElement.prototype.getContext

// jsdom 未实现 matchMedia，主题模块与 inline 脚本需要兜底
if (typeof window.EventSource !== 'function') {
  window.EventSource = class {
    url: string
    withCredentials: boolean
    readyState = 0
    onerror: ((ev: Event) => void) | null = null
    onmessage: ((ev: MessageEvent) => void) | null = null
    onopen: ((ev: Event) => void) | null = null
    constructor(url: string, init?: EventSourceInit) {
      this.url = url
      this.withCredentials = Boolean(init?.withCredentials)
    }
    addEventListener(): void {}
    removeEventListener(): void {}
    close(): void {}
    dispatchEvent(): boolean { return false }
  } as unknown as typeof EventSource
}

if (typeof window.matchMedia !== 'function') {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}
