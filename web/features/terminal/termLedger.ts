/** 终端按键攒成一行，回车后进瀑布流。方向键/焦点报告不当消息。 */

export function feedTermLedger(
  buffer: string,
  chunk: string,
): { buffer: string; line: string | null } {
  if (!chunk) return { buffer, line: null }
  if (chunk === '\x1b[I' || chunk === '\x1b[O') return { buffer, line: null }
  if (chunk.startsWith('\x1b')) return { buffer: '', line: null }
  if (chunk === '\x03' || chunk === '\x04') return { buffer: '', line: null }
  let buf = buffer
  let line: string | null = null
  for (const ch of chunk) {
    if (ch === '\r' || ch === '\n') {
      const text = buf.trim()
      buf = ''
      if (text) line = text
      continue
    }
    if (ch === '\x7f' || ch === '\b') {
      buf = buf.slice(0, -1)
      continue
    }
    if (ch >= ' ' || ch === '\t') buf += ch
  }
  return { buffer: buf, line }
}
