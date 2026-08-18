import { vi } from 'vitest'
import { concatTail, createLoadGate, createTermWriter, REPLAY_TAIL, RENDER_CHUNK, replayTail } from '../features/terminal/termRender'

describe('termRender', () => {
  it('回放只保留尾部 8 KiB', () => {
    const data = new Uint8Array(20 * 1024)
    data[data.length - 1] = 7
    const tail = replayTail(data)
    expect(tail.byteLength).toBe(REPLAY_TAIL)
    expect(tail[tail.length - 1]).toBe(7)
  })

  it('多帧回放合并后仍只留尾部', () => {
    const first = new Uint8Array(6 * 1024).fill(1)
    const second = new Uint8Array(6 * 1024).fill(2)
    const tail = concatTail(first, second)
    expect(tail.byteLength).toBe(REPLAY_TAIL)
    expect(tail[0]).toBe(1)
    expect(tail[tail.length - 1]).toBe(2)
  })

  it('写入按 16 KiB 分片并让步', async () => {
    const sizes: number[] = []
    const writer = createTermWriter((data, done) => {
      sizes.push(data.byteLength)
      done?.()
    })
    writer.queue(new Uint8Array(20 * 1024))
    await vi.waitFor(() => {
      expect(sizes).toEqual([RENDER_CHUNK, 4 * 1024])
    })
    writer.dispose()
  })

  it('回放完成后要等安静窗口才揭开，中途输出会再挡住', async () => {
    vi.useFakeTimers()
    const ready = vi.fn()
    const gate = createLoadGate({
      isBusy: () => false,
      onReady: ready,
      quietMs: 80,
    })
    gate.noteReplayComplete()
    expect(ready).not.toHaveBeenCalled()
    vi.advanceTimersByTime(40)
    gate.noteOutput()
    vi.advanceTimersByTime(40)
    expect(ready).not.toHaveBeenCalled()
    vi.advanceTimersByTime(80)
    expect(ready).toHaveBeenCalledOnce()
    gate.dispose()
    vi.useRealTimers()
  })
})
