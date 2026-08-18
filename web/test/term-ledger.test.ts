import { describe, expect, it } from 'vitest'
import { feedTermLedger } from '../features/terminal/termLedger'

describe('feedTermLedger', () => {
  it('攒字后回车交出一行', () => {
    let buf = ''
    let out = feedTermLedger(buf, '你')
    buf = out.buffer
    out = feedTermLedger(buf, '好')
    buf = out.buffer
    out = feedTermLedger(buf, '\r')
    expect(out.line).toBe('你好')
    expect(out.buffer).toBe('')
  })

  it('整行加回车一次交出', () => {
    expect(feedTermLedger('', '复盘这一句\r')).toEqual({ buffer: '', line: '复盘这一句' })
  })

  it('方向键和焦点报告不当消息', () => {
    expect(feedTermLedger('草稿', '\x1b[A')).toEqual({ buffer: '', line: null })
    expect(feedTermLedger('草稿', '\x1b[I')).toEqual({ buffer: '草稿', line: null })
  })

  it('空回车不交行', () => {
    expect(feedTermLedger('   ', '\r')).toEqual({ buffer: '', line: null })
  })
})
