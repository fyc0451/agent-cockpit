// 最右栏：项目目录树（懒加载 + 搜索）。点击文件上抛 onPreview，由主区展示预览，本栏不渲染内容。

import { useCallback, useEffect, useState } from 'react'
import { type DirEntry, type SearchResult } from '../../api/legacyFiles'
import { fetchSessionDirList, searchSessionFiles } from '../../api/chatSession'

interface FilePanelProps {
  session: string
  root: string
  open: boolean // 窄屏抽屉态
  /** 详情栏 embedded 态：隐藏自带头部（tab 条已有标签与关闭钮）。 */
  embedded?: boolean
  onPreview: (path: string) => void
  onClose: () => void
}

interface TreeState {
  expanded: Record<string, boolean>
  children: Record<string, DirEntry[]>
  loading: Record<string, boolean>
}

function joinPath(parent: string, name: string): string {
  return parent.endsWith('/') ? parent + name : `${parent}/${name}`
}

function baseName(path: string): string {
  return path.replace(/\/+$/, '').split('/').pop() || path
}

function TreeNode({
  path,
  name,
  type,
  depth,
  state,
  selected,
  onToggle,
  onOpen,
}: {
  path: string
  name: string
  type: string
  depth: number
  state: TreeState
  selected: string | null
  onToggle: (path: string) => void
  onOpen: (path: string) => void
}) {
  const isDir = type === 'dir'
  const expanded = !!state.expanded[path]
  const kids = state.children[path]
  return (
    <div>
      <button
        type="button"
        className={`gc-tree-row${selected === path ? ' is-selected' : ''}`}
        style={{ paddingLeft: 7 + depth * 14 }}
        onClick={() => (isDir ? onToggle(path) : onOpen(path))}
        title={path}
      >
        <span className="gc-tree-caret" aria-hidden>
          {isDir ? (state.loading[path] ? '…' : expanded ? '▾' : '▸') : ''}
        </span>
        <span aria-hidden>{isDir ? '📁' : '📄'}</span>
        <span className="gc-tree-name">{name}</span>
      </button>
      {isDir && expanded && kids && (
        <div>
          {kids.map((e) => (
            <TreeNode
              key={e.name}
              path={joinPath(path, e.name)}
              name={e.name}
              type={e.type}
              depth={depth + 1}
              state={state}
              selected={selected}
              onToggle={onToggle}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function FilePanel({ session, root, open, embedded, onPreview, onClose }: FilePanelProps) {
  const [tree, setTree] = useState<TreeState>({ expanded: {}, children: {}, loading: {} })
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  // 根目录首层：固定为当前会话/项目目录
  useEffect(() => {
    setTree({ expanded: { [root]: true }, children: {}, loading: { [root]: true } })
    setResults(null)
    fetchSessionDirList(session, root)
      .then((list) => {
        setTree((t) => ({
          ...t,
          children: { ...t.children, [root]: list.entries },
          loading: { ...t.loading, [root]: false },
        }))
      })
      .catch(() => {
        setTree((t) => ({ ...t, loading: { ...t.loading, [root]: false } }))
      })
  }, [session, root])

  // 搜索（防抖 300ms）
  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setResults(null)
      return
    }
    const timer = window.setTimeout(() => {
      searchSessionFiles(session, q)
        .then(setResults)
        .catch(() => setResults([]))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query, session])

  const toggle = useCallback((path: string) => {
    setTree((t) => {
      const expanded = { ...t.expanded, [path]: !t.expanded[path] }
      const next = { ...t, expanded }
      if (expanded[path] && !t.children[path]) {
        next.loading = { ...t.loading, [path]: true }
        fetchSessionDirList(session, path)
          .then((list) => {
            setTree((cur) => ({
              ...cur,
              children: { ...cur.children, [path]: list.entries },
              loading: { ...cur.loading, [path]: false },
            }))
          })
          .catch(() => {
            setTree((cur) => ({ ...cur, loading: { ...cur.loading, [path]: false } }))
          })
      }
      return next
    })
  }, [session])

  const openFile = useCallback(
    (path: string) => {
      setSelected(path)
      onPreview(path)
    },
    [onPreview],
  )

  return (
    <aside
      className={`gc-files${open ? ' is-open' : ''}${embedded ? ' gc-files--embedded' : ''}`}
      aria-label="项目目录"
    >
      {!embedded && (
        <div className="gc-files-head">
          <span className="gc-files-title" title={root ?? ''}>
            📁 {root ? baseName(root) : '目录'}
          </span>
          <button type="button" className="gc-icon-btn" title="隐藏目录树" onClick={onClose}>
            ✕
          </button>
        </div>
      )}

      <input
        className="gc-files-search"
        placeholder="搜索文件名或目录…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {results ? (
        <div className="gc-files-tree">
          {results.length === 0 && <div className="gc-side-empty">没有匹配的文件</div>}
          {results.map((r) => (
            <button
              key={r.path}
              type="button"
              className="gc-tree-row"
              onClick={() => (r.type === 'dir' ? undefined : openFile(r.path))}
              title={r.path}
            >
              <span className="gc-tree-caret" aria-hidden />
              <span aria-hidden>{r.type === 'dir' ? '📁' : '📄'}</span>
              <span className="gc-tree-name">{r.relative || r.name}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="gc-files-tree">
          {root && tree.children[root] ? (
            tree.children[root].map((e) => (
              <TreeNode
                key={e.name}
                path={joinPath(root, e.name)}
                name={e.name}
                type={e.type}
                depth={0}
                state={tree}
                selected={selected}
                onToggle={toggle}
                onOpen={openFile}
              />
            ))
          ) : (
            <div className="gc-side-empty">加载中…</div>
          )}
        </div>
      )}
    </aside>
  )
}
