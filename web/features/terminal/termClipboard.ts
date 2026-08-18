const MAX_OSC52_ENCODED = 1_400_000
const MAX_OSC52_BYTES = 1_000_000

/** xterm OSC 52 payload: `<selection>;<base64>`。查询 `?` 不视为复制内容。 */
export function decodeOsc52(data: string): string | null {
  const split = data.indexOf(';')
  if (split < 0) return null
  const encoded = data.slice(split + 1)
  if (!encoded || encoded === '?' || encoded.length > MAX_OSC52_ENCODED) return null
  try {
    const binary = atob(encoded)
    if (binary.length > MAX_OSC52_BYTES) return null
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return null
  }
}

/** 用户点击按钮后写浏览器剪贴板；HTTP 环境回退到 execCommand。 */
export async function writeBrowserClipboard(text: string): Promise<boolean> {
  if (window.isSecureContext && typeof navigator.clipboard?.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 继续尝试用户手势内的兼容回退。
    }
  }
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const area = document.createElement('textarea')
  area.value = text
  area.readOnly = true
  area.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0'
  document.body.appendChild(area)
  area.select()
  let copied = false
  try {
    copied = typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    copied = false
  }
  area.remove()
  active?.focus()
  return copied
}
