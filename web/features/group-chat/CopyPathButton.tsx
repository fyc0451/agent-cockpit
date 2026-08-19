import { useState } from 'react'
import { writeBrowserClipboard } from '../terminal/termClipboard'

export function CopyPathButton({
  path,
  className,
  label = '复制路径',
}: {
  path: string
  className?: string
  label?: string
}) {
  const [note, setNote] = useState<'idle' | 'ok' | 'fail'>('idle')

  return (
    <button
      type="button"
      className={className}
      title={`复制路径 ${path}`}
      aria-label={`复制路径 ${path}`}
      onClick={(event) => {
        event.stopPropagation()
        void writeBrowserClipboard(path).then((ok) => {
          setNote(ok ? 'ok' : 'fail')
          window.setTimeout(() => setNote('idle'), 1500)
        })
      }}
    >
      {note === 'ok' ? '已复制' : note === 'fail' ? '复制失败' : label}
    </button>
  )
}
