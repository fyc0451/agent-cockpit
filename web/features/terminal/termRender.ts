import { WebglAddon } from '@xterm/addon-webgl'

// 1.0 已验证：回放只写尾部 8 KiB，16 KiB 分片 + setTimeout(0)；不要 1 KiB 微分片。
export const REPLAY_TAIL = 8 * 1024
export const RENDER_CHUNK = 16 * 1024

export function replayTail(data: Uint8Array, max = REPLAY_TAIL): Uint8Array {
  if (data.byteLength <= max) return data
  return data.subarray(data.byteLength - max)
}

export function concatTail(prev: Uint8Array, next: Uint8Array, max = REPLAY_TAIL): Uint8Array {
  if (!next.byteLength) return prev.byteLength > max ? replayTail(prev, max) : prev
  if (!prev.byteLength) return replayTail(next, max)
  const total = prev.byteLength + next.byteLength
  if (total <= max) {
    const out = new Uint8Array(total)
    out.set(prev, 0)
    out.set(next, prev.byteLength)
    return out
  }
  const out = new Uint8Array(max)
  const keepPrev = Math.max(0, max - next.byteLength)
  if (keepPrev) out.set(prev.subarray(prev.byteLength - keepPrev), 0)
  out.set(next.subarray(next.byteLength - (max - keepPrev)), keepPrev)
  return out
}

export interface TermWriter {
  queue: (data: Uint8Array) => void
  clear: () => void
  busy: () => boolean
  setOnIdle: (cb: (() => void) | undefined) => void
  dispose: () => void
}

export function createTermWriter(
  write: (data: Uint8Array, done?: () => void) => void,
  onIdle?: () => void,
): TermWriter {
  const queue: Array<{ data: Uint8Array; offset: number }> = []
  let rendering = false
  let disposed = false
  let idleCb = onIdle

  const drained = () => {
    if (!disposed && !rendering && !queue.length) idleCb?.()
  }

  const pump = () => {
    if (disposed || rendering || !queue.length) {
      drained()
      return
    }
    const item = queue[0]
    rendering = true
    const end = Math.min(item.offset + RENDER_CHUNK, item.data.byteLength)
    try {
      write(item.data.subarray(item.offset, end), () => {
        if (disposed) return
        item.offset = end
        rendering = false
        if (item.offset >= item.data.byteLength) queue.shift()
        setTimeout(pump, 0)
      })
    } catch {
      rendering = false
      queue.shift()
      setTimeout(pump, 0)
    }
  }

  return {
    queue(data) {
      if (disposed || !data.byteLength) return
      queue.push({ data, offset: 0 })
      pump()
    },
    clear() {
      queue.length = 0
      rendering = false
    },
    busy() {
      return rendering || queue.length > 0
    },
    setOnIdle(cb) {
      idleCb = cb
    },
    dispose() {
      disposed = true
      queue.length = 0
    },
  }
}

export const LOADING_QUIET_MS = 80
export const TUI_WAIT_MS = 1500

/** 回放完、队列空、TUI 进备用屏后再揭开。提前揭开点击会进 PTY，几秒后幽灵回放。 */
export function createLoadGate(opts: {
  isBusy: () => boolean
  isAlternate?: () => boolean
  onReady: () => void
  quietMs?: number
  tuiWaitMs?: number
}): {
  noteOutput: () => void
  noteReplayComplete: () => void
  noteIdle: () => void
  dispose: () => void
} {
  const quietMs = opts.quietMs ?? LOADING_QUIET_MS
  const tuiWaitMs = opts.tuiWaitMs ?? TUI_WAIT_MS
  let replayComplete = false
  let waitTui = false
  let quietTimer: ReturnType<typeof setTimeout> | null = null
  let tuiTimer: ReturnType<typeof setTimeout> | null = null
  let ready = false
  let disposed = false

  const finish = () => {
    if (ready || disposed) return
    ready = true
    if (quietTimer) clearTimeout(quietTimer)
    if (tuiTimer) clearTimeout(tuiTimer)
    quietTimer = null
    tuiTimer = null
    opts.onReady()
  }

  const armQuiet = () => {
    if (quietTimer) clearTimeout(quietTimer)
    quietTimer = setTimeout(() => {
      quietTimer = null
      if (ready || disposed) return
      if (!replayComplete || opts.isBusy()) return
      if (waitTui && opts.isAlternate && !opts.isAlternate()) return
      waitTui = false
      finish()
    }, quietMs)
  }

  const settle = (force = false) => {
    if (ready || disposed) return
    if (force) {
      finish()
      return
    }
    if (!replayComplete || opts.isBusy()) return
    if (waitTui && opts.isAlternate && !opts.isAlternate()) return
    waitTui = false
    armQuiet()
  }

  return {
    noteOutput() {
      if (quietTimer) {
        clearTimeout(quietTimer)
        quietTimer = null
      }
      if (replayComplete) armQuiet()
    },
    noteReplayComplete() {
      replayComplete = true
      waitTui = typeof opts.isAlternate === 'function'
      if (waitTui) {
        tuiTimer = setTimeout(() => settle(true), tuiWaitMs)
      }
      settle()
    },
    noteIdle() {
      settle()
    },
    dispose() {
      disposed = true
      if (quietTimer) clearTimeout(quietTimer)
      if (tuiTimer) clearTimeout(tuiTimer)
    },
  }
}

export function attachWebgl(term: {
  loadAddon: (addon: WebglAddon) => void
  refresh: (start: number, end: number) => void
  rows: number
}): () => void {
  let webgl: WebglAddon | null = null
  try {
    webgl = new WebglAddon()
    webgl.onContextLoss(() => {
      try {
        webgl?.dispose()
      } catch {
        // 忽略
      }
      webgl = null
      try {
        term.refresh(0, term.rows - 1)
      } catch {
        // 忽略
      }
    })
    term.loadAddon(webgl)
  } catch {
    try {
      webgl?.dispose()
    } catch {
      // 忽略
    }
    webgl = null
  }
  return () => {
    try {
      webgl?.dispose()
    } catch {
      // 忽略
    }
    webgl = null
  }
}
