// 主区文件预览：点击目录树文件后，预览占用瀑布流位置，可关闭返回群聊。

import { useEffect, useState } from 'react'
import { type FileRead } from '../../api/legacyFiles'
import {
  fetchSessionFileContent,
  sessionFileDownloadUrl,
  sessionFileRawUrl,
} from '../../api/chatSession'
import { CopyPathButton } from './CopyPathButton'

const PREVIEW_MAX_CHARS = 60_000
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|ico|avif)$/i

function fileLabel(path: string): string {
  const name = path.split('/').pop() || path
  const stamped = name.match(/^\d{10,}-[0-9a-f]{6,}-(.+)$/i)
  return stamped ? stamped[1] : name
}

export function FilePreview({
  session,
  path,
  onClose,
}: {
  session: string
  path: string
  onClose: () => void
}) {
  const [data, setData] = useState<FileRead | null>(null)
  const [error, setError] = useState<string | null>(null)
  const image = IMAGE_EXT.test(path)

  useEffect(() => {
    setData(null)
    setError(null)
    if (image) return
    fetchSessionFileContent(session, path)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [session, path, image])

  const name = fileLabel(path)
  const downloadHref = sessionFileDownloadUrl(session, path)

  return (
    <div className="gc-fileview" aria-label="文件预览">
      <div className="gc-fileview-head">
        <span className="gc-fileview-name" title={path}>
          {image ? '🖼' : '📄'} {name}
        </span>
        <span className="gc-fileview-path" title={path}>{path}</span>
        <CopyPathButton path={path} className="gc-pill-btn" />
        <a className="gc-pill-btn" href={downloadHref} download>
          ⬇ 下载
        </a>
        <button type="button" className="gc-icon-btn" title="关闭预览" onClick={onClose}>
          ✕
        </button>
      </div>
      {image ? (
        <div className="gc-fileview-body gc-fileview-media">
          <img className="gc-fileview-image" src={sessionFileRawUrl(session, path)} alt={name} />
        </div>
      ) : error ? (
        <div className="gc-fileview-body">
          <div className="gc-modal-error">{error}</div>
        </div>
      ) : !data ? (
        <div className="gc-fileview-body gc-empty-hint">加载中…</div>
      ) : data.binary ? (
        <div className="gc-fileview-body gc-empty-hint">
          二进制文件（{data.size} bytes），不支持预览，请下载查看。
        </div>
      ) : (
        <pre className="gc-fileview-body">
          {data.text.length > PREVIEW_MAX_CHARS
            ? `${data.text.slice(0, PREVIEW_MAX_CHARS)}\n\n…（超长截断，完整内容请下载）`
            : data.text}
        </pre>
      )}
    </div>
  )
}
