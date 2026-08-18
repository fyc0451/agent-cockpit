import { useEffect, useState } from 'react'
import type { DirEntry, DirList, FileRead } from '../../api/legacyFiles'
import { fetchSessionDirList, fetchSessionFileContent } from '../../api/chatSession'

interface HerdrTerminalFilePanelProps {
  session: string
  root: string
  onClose: () => void
}

function joinPath(parent: string, name: string): string {
  return parent.endsWith('/') ? `${parent}${name}` : `${parent}/${name}`
}

function parentWithinRoot(path: string, root: string): string | null {
  const cleanPath = path.replace(/\/+$/, '') || '/'
  const cleanRoot = root.replace(/\/+$/, '') || '/'
  if (cleanPath === cleanRoot) return null
  const parent = cleanPath.slice(0, cleanPath.lastIndexOf('/')) || '/'
  return parent === cleanRoot || parent.startsWith(`${cleanRoot}/`) ? parent : cleanRoot
}

function messageOf(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause)
}

export function HerdrTerminalFilePanel({ session, root, onClose }: HerdrTerminalFilePanelProps) {
  const [cwd, setCwd] = useState(root)
  const [listing, setListing] = useState<DirList | null>(null)
  const [preview, setPreview] = useState<FileRead | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setPreview(null)
    fetchSessionDirList(session, cwd)
      .then((next) => {
        if (cancelled) return
        setListing(next)
        setCwd(next.path || cwd)
      })
      .catch((cause) => {
        if (!cancelled) setError(messageOf(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [cwd, session])

  const openFile = async (entry: DirEntry) => {
    const path = joinPath(cwd, entry.name)
    setLoading(true)
    setError(null)
    try {
      setPreview(await fetchSessionFileContent(session, path))
    } catch (cause) {
      setError(messageOf(cause))
    } finally {
      setLoading(false)
    }
  }

  const parent = parentWithinRoot(cwd, root)

  return (
    <aside className="gc-term-files" aria-label="终端文件">
      <div className="gc-term-files-head">
        <span className="gc-term-files-path" title={preview?.path || cwd}>
          📁 {preview?.path || cwd}
        </span>
        <button
          type="button"
          className="gc-terminal-head-button"
          onClick={onClose}
          aria-label="关闭文件"
          title="关闭文件"
        >
          ×
        </button>
      </div>

      {preview ? (
        <div className="gc-term-file-preview">
          <button
            type="button"
            className="gc-term-file-back"
            onClick={() => setPreview(null)}
            aria-label="返回文件列表"
          >
            ← 返回列表
          </button>
          {preview.binary ? (
            <div className="gc-term-file-empty">二进制文件（{preview.size} bytes），不支持预览。</div>
          ) : (
            <pre>{preview.text}</pre>
          )}
        </div>
      ) : (
        <div className="gc-term-file-list">
          <button
            type="button"
            className="gc-term-file-row"
            onClick={() => parent && setCwd(parent)}
            disabled={!parent}
            aria-label="返回上级目录"
          >
            <span aria-hidden>↰</span>
            <span>上级目录</span>
          </button>
          {loading && <div className="gc-term-file-empty">加载中…</div>}
          {!loading && error && <div className="gc-term-file-empty">打开目录失败：{error}</div>}
          {!loading && !error && listing?.entries.length === 0 && (
            <div className="gc-term-file-empty">空目录</div>
          )}
          {!loading && !error && listing?.entries.map((entry) => (
            <button
              key={entry.name}
              type="button"
              className="gc-term-file-row"
              onClick={() => {
                if (entry.type === 'dir') setCwd(joinPath(cwd, entry.name))
                else void openFile(entry)
              }}
              aria-label={`${entry.type === 'dir' ? '进入' : '打开'} ${entry.name}`}
              title={entry.name}
            >
              <span aria-hidden>{entry.type === 'dir' ? '📁' : '📄'}</span>
              <span>{entry.name}</span>
            </button>
          ))}
        </div>
      )}
    </aside>
  )
}
