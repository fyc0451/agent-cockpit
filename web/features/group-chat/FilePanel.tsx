// 最右栏：项目目录树（懒加载 + 搜索）。点击文件上抛 onPreview，由主区展示预览，本栏不渲染内容。

import { useCallback, useEffect, useState } from 'react'
import { type DirEntry, type SearchResult } from '../../api/legacyFiles'
import { fetchSessionDirList, fetchSessionGit, searchSessionFiles, type SessionGitSummary } from '../../api/chatSession'
import { CopyPathButton } from './CopyPathButton'

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
      <div className={`gc-tree-row${selected === path ? ' is-selected' : ''}`}>
        <button
          type="button"
          className="gc-tree-main"
          style={{ paddingLeft: 7 + depth * 14 }}
          onClick={() => (isDir ? onToggle(path) : onOpen(path))}
          title={path}
        >
          <span className="gc-tree-caret" aria-hidden>
            {isDir ? (state.loading[path] ? '…' : expanded ? '▾' : '▸') : ''}
          </span>
          <span aria-hidden>{isDir ? '📁' : '📄'}</span>
          <span className="gc-tree-name">{displayName(name)}</span>
        </button>
        <CopyPathButton path={path} className="gc-tree-copy" label="复制" />
      </div>
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

const INBOX_NAME = 'cockpit-inbox'

function inboxPath(root: string): string {
  return joinPath(root, INBOX_NAME)
}

function displayName(name: string): string {
  if (name === INBOX_NAME) return '群聊附件'
  const stamped = name.match(/^\d{10,}-[0-9a-f]{6,}-(.+)$/i)
  return stamped ? stamped[1] : name
}

function newestFirst(entries: DirEntry[]): DirEntry[] {
  return [...entries].sort((a, b) => b.name.localeCompare(a.name))
}

function WorkspaceGitCard({ session }: { session: string }) {
  const [summary, setSummary] = useState<SessionGitSummary | null>(null)
  const [view, setView] = useState<'closed' | 'stat' | 'diff' | 'branches'>('closed')
  const [diff, setDiff] = useState<string | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setSummary(null)
    setView('closed')
    setDiff(null)
    fetchSessionGit(session)
      .then((next) => {
        if (!cancelled) setSummary(next)
      })
      .catch(() => {
        if (!cancelled) setSummary({ repo: false, branch: '', branches: [], files: 0, stat: '' })
      })
    return () => {
      cancelled = true
    }
  }, [session])

  const loadDiff = useCallback(() => {
    setView('diff')
    if (diff != null || diffLoading) return
    setDiffLoading(true)
    fetchSessionGit(session, { diff: true })
      .then((next) => {
        setSummary((prev) => prev ? { ...prev, ...next } : next)
        setDiff(next.diff ?? '')
      })
      .catch(() => {
        setDiff('')
      })
      .finally(() => {
        setDiffLoading(false)
      })
  }, [diff, diffLoading, session])

  if (!summary) {
    return (
      <section className="gc-git-summary" aria-label="工作区 git">
        <p className="gc-git-summary-note">正在读取工作区 git…</p>
      </section>
    )
  }
  if (!summary.repo) {
    return (
      <section className="gc-git-summary" aria-label="工作区 git">
        <p className="gc-git-summary-note">
          这里看整个工作区相对当前分支的未提交改动，不是某条聊天气泡的变更。当前目录不是 git 仓库。
        </p>
      </section>
    )
  }

  const dirty = summary.files > 0
  const extra = summary.branches.filter((name) => name !== summary.branch)
  const body = view === 'diff'
    ? (diffLoading ? '加载 diff…' : (diff || '没有可显示的 diff。'))
    : view === 'branches'
      ? [summary.branch && `* ${summary.branch}`, ...extra].filter(Boolean).join('\n') || '没有本地分支。'
      : summary.stat
  return (
    <section className="gc-git-summary" aria-label="工作区 git">
      <p className="gc-git-summary-note">
        这里看整个工作区相对当前分支的未提交改动，不是某条聊天气泡的变更。
      </p>
      <div className="gc-git-summary-row">
        <span className="gc-git-summary-branch" title={summary.branch}>
          {summary.branch || '未知分支'}
        </span>
        <span className="gc-git-summary-count">
          {dirty ? `${summary.files} 个文件有改动` : '工作区干净'}
        </span>
      </div>
      <div className="gc-git-summary-actions">
        <button
          type="button"
          className="gc-git-summary-toggle"
          aria-expanded={view === 'branches'}
          onClick={() => setView((cur) => (cur === 'branches' ? 'closed' : 'branches'))}
        >
          {view === 'branches' ? '收起分支' : '查看分支'}
        </button>
        {dirty && (
          <>
            <button
              type="button"
              className="gc-git-summary-toggle"
              aria-expanded={view === 'stat'}
              onClick={() => setView((cur) => (cur === 'stat' ? 'closed' : 'stat'))}
            >
              {view === 'stat' ? '收起 stat' : '查看 stat'}
            </button>
            <button
              type="button"
              className="gc-git-summary-toggle"
              aria-expanded={view === 'diff'}
              onClick={() => (view === 'diff' ? setView('closed') : loadDiff())}
            >
              {view === 'diff' ? '收起 diff' : '查看 diff'}
            </button>
          </>
        )}
      </div>
      {view !== 'closed' && (view === 'branches' || dirty) && (
        <pre className="gc-git-summary-body">{body}</pre>
      )}
    </section>
  )
}

export function FilePanel({ session, root, open, embedded, onPreview, onClose }: FilePanelProps) {
  const [tree, setTree] = useState<TreeState>({ expanded: {}, children: {}, loading: {} })
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const inbox = inboxPath(root)

  // 根目录首层：固定为当前会话/项目目录。群聊附件默认收起，点开再列。
  useEffect(() => {
    setTree({
      expanded: { [root]: true },
      children: {},
      loading: { [root]: true },
    })
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

      <WorkspaceGitCard session={session} />

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
            <div key={r.path} className="gc-tree-row">
              <button
                type="button"
                className="gc-tree-main"
                onClick={() => (r.type === 'dir' ? undefined : openFile(r.path))}
                title={r.path}
              >
                <span className="gc-tree-caret" aria-hidden />
                <span aria-hidden>{r.type === 'dir' ? '📁' : '📄'}</span>
                <span className="gc-tree-name">{r.relative || displayName(r.name)}</span>
              </button>
              <CopyPathButton path={r.path} className="gc-tree-copy" label="复制" />
            </div>
          ))}
        </div>
      ) : (
        <div className="gc-files-tree">
          <div className="gc-inbox" role="region" aria-label="群聊附件">
            <div className={`gc-tree-row${selected === inbox ? ' is-selected' : ''}`}>
              <button
                type="button"
                className="gc-tree-main"
                aria-expanded={!!tree.expanded[inbox]}
                onClick={() => toggle(inbox)}
                title={`${inbox} · 本群上传的截图和文件，不进 git`}
              >
                <span className="gc-tree-caret" aria-hidden>
                  {tree.loading[inbox] ? '…' : tree.expanded[inbox] ? '▾' : '▸'}
                </span>
                <span aria-hidden>📎</span>
                <span className="gc-tree-name">群聊附件</span>
              </button>
              <CopyPathButton path={inbox} className="gc-tree-copy" label="复制" />
            </div>
            {tree.expanded[inbox] && (
              <div>
                {tree.loading[inbox] && !tree.children[inbox] && (
                  <div className="gc-side-empty">加载中…</div>
                )}
                {tree.children[inbox] && tree.children[inbox].length === 0 && (
                  <div className="gc-side-empty">还没有上传过附件</div>
                )}
                {newestFirst(tree.children[inbox] ?? []).map((e) => (
                  <TreeNode
                    key={e.name}
                    path={joinPath(inbox, e.name)}
                    name={e.name}
                    type={e.type}
                    depth={1}
                    state={tree}
                    selected={selected}
                    onToggle={toggle}
                    onOpen={openFile}
                  />
                ))}
              </div>
            )}
          </div>
          {root && tree.children[root] ? (
            tree.children[root]
              .filter((e) => e.name !== INBOX_NAME)
              .map((e) => (
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
