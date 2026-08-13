import { useMemo, useState, type FormEvent } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  useWorkspaceFileContent,
  useWorkspaceFileSearch,
  useWorkspaceFiles,
} from '../api/localSlice'
import type { WorkspaceFileEntry, WorkspaceFileSearchResult } from '../api/localSlice'
import { isDegraded } from '../api/normalize'
import type { Project, Workspace } from '../api/types'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { routeHrefs } from '../app/routes'
import { useCapability, workspaceScope } from '../state/capabilities'

/** 控制字符（NUL、<0x20、0x7f）检查，与后端合同一致 */
function hasControlChar(p: string): boolean {
  for (let i = 0; i < p.length; i++) {
    const c = p.charCodeAt(i)
    if (c < 0x20 || c === 0x7f) return true
  }
  return false
}

/** 只允许规范 POSIX 相对路径进请求：禁绝对/~/反斜杠/控制字符/空段/./../尾斜杠 */
export function isSafeRelativePath(p: string): boolean {
  if (p === '') return true
  if (p.startsWith('/') || p.startsWith('~') || p.includes('\\') || p.endsWith('/')) return false
  if (hasControlChar(p)) return false
  return p.split('/').every((seg) => seg !== '' && seg !== '.' && seg !== '..')
}

function parentPath(p: string): string {
  const idx = p.lastIndexOf('/')
  return idx < 0 ? '' : p.slice(0, idx)
}

function basename(p: string): string {
  const idx = p.lastIndexOf('/')
  return idx < 0 ? p : p.slice(idx + 1)
}

/** tree entry 无 path：相对路径由当前目录 path + name 组装（合同） */
function joinPath(dir: string, name: string): string {
  return dir === '' ? name : `${dir}/${name}`
}

function EntryRow({
  name,
  type,
  fullPath,
  onOpen,
}: {
  name: string
  type: 'dir' | 'file'
  fullPath: string
  onOpen: () => void
}) {
  return (
    <li className="list-row">
      <button type="button" className="list-row-main list-link btn-link" onClick={onOpen}>
        <span className="ellipsis list-title">{type === 'dir' ? `${name}/` : name}</span>
        <span className="ellipsis list-sub">{fullPath}</span>
      </button>
      <span className="list-sub">{type === 'dir' ? '目录' : '文件'}</span>
    </li>
  )
}

function FilesBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const path = searchParams.get('path') ?? ''
  const file = searchParams.get('file')
  const q = searchParams.get('q') ?? ''
  const [searchInput, setSearchInput] = useState(q)

  const slug = project.slug ?? ''
  const workspaceId = workspace.id ?? ''
  const cap = useCapability('files.read', workspaceScope(slug, workspaceId))

  // URL 深链携带非法路径 → typed error，零请求（不得透传后端）
  const pathSafe = isSafeRelativePath(path) && (file == null || isSafeRelativePath(file))

  const list = useWorkspaceFiles(
    project.project_id ?? null,
    workspaceId || null,
    path,
    slug || null,
    cap.available && pathSafe && q === '',
  )
  const content = useWorkspaceFileContent(
    project.project_id ?? null,
    workspaceId || null,
    file,
    slug || null,
    cap.available && pathSafe && file != null,
  )
  const search = useWorkspaceFileSearch(
    project.project_id ?? null,
    workspaceId || null,
    path,
    q,
    slug || null,
    cap.available && pathSafe && q !== '',
  )

  const crumbs = useMemo(() => (path === '' ? [] : path.split('/')), [path])

  if (!cap.available) {
    return (
      <>
        <PageHeader title="文件" sub={workspace.name ?? workspace.id} />
        <StatusState
          kind="forbidden"
          title="文件浏览暂不可用"
          reason={cap.reason}
          docsRoute={cap.docsRoute ?? routeHrefs.doctor()}
        />
      </>
    )
  }

  if (!pathSafe) {
    return (
      <>
        <PageHeader title="文件" sub={workspace.name ?? workspace.id} />
        <StatusState
          kind="error"
          title="非法路径"
          description="只允许相对路径；已拒绝向服务端发送该路径。"
          action={{ label: '回到根目录', onClick: () => setSearchParams({}) }}
        />
      </>
    )
  }

  const openEntry = (entry: WorkspaceFileEntry | WorkspaceFileSearchResult, fullPath: string) => {
    if (!isSafeRelativePath(fullPath)) return
    if (entry.type === 'dir') {
      setSearchParams({ path: fullPath })
    } else {
      setSearchParams(q ? { path, q, file: fullPath } : { path, file: fullPath })
    }
  }

  const submitSearch = (e: FormEvent) => {
    e.preventDefault()
    const value = searchInput.trim()
    setSearchParams(value ? { path, q: value } : { path })
  }

  const searching = q !== ''

  return (
    <>
      <PageHeader title="文件" sub={workspace.name ?? workspace.id} />
      <section className="panel">
        <div className="state-actions files-toolbar">
          <nav aria-label="目录路径" className="breadcrumb">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setSearchParams({})}
              disabled={path === '' && !searching}
            >
              根目录
            </button>
            {crumbs.map((seg, i) => (
              <span key={i}>
                <span aria-hidden="true">/</span>
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => setSearchParams({ path: crumbs.slice(0, i + 1).join('/') })}
                >
                  {seg}
                </button>
              </span>
            ))}
          </nav>
          <form role="search" onSubmit={submitSearch} className="state-actions">
            <input
              type="search"
              aria-label="搜索文件"
              placeholder="搜索文件名…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
            <button type="submit" className="btn btn--primary">
              搜索
            </button>
            {searching ? (
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  setSearchInput('')
                  setSearchParams({ path })
                }}
              >
                清除
              </button>
            ) : null}
          </form>
        </div>

        {searching ? (
          search.isPending ? (
            <StatusState kind="loading" title="正在搜索…" />
          ) : search.isError ? (
            <QueryErrorState error={search.error} onRetry={() => search.refetch()} />
          ) : (
            <>
              {search.data!.data.truncated ? (
                <StatusState
                  kind="degraded"
                  banner
                  title="搜索结果已截断"
                  description="结果过多，仅显示前 50 条；请缩小目录或更换关键词。"
                />
              ) : null}
              {search.data!.data.results.length === 0 ? (
                <StatusState kind="empty" title="没有匹配的文件" description={`关键词「${q}」在当前目录下无结果。`} />
              ) : (
                <ul className="list" aria-label={`搜索 ${q} 的结果`}>
                  {search.data!.data.results.map((entry) => (
                    <EntryRow
                      key={entry.path}
                      name={entry.name}
                      type={entry.type}
                      fullPath={entry.path}
                      onOpen={() => openEntry(entry, entry.path)}
                    />
                  ))}
                </ul>
              )}
            </>
          )
        ) : list.isPending ? (
          <StatusState kind="loading" title="正在加载目录…" />
        ) : list.isError ? (
          <QueryErrorState error={list.error} onRetry={() => list.refetch()} />
        ) : (
          <>
            {isDegraded(list.data!.meta) ? (
              <StatusState
                kind="degraded"
                banner
                title="目录数据不完整"
                description="部分来源不可用，列表可能不完整。"
              />
            ) : null}
            {list.data!.data.entries.length === 0 ? (
              isDegraded(list.data!.meta) ? (
                <StatusState kind="degraded" title="部分数据不可用" description="来源异常，暂无可展示的完整数据。" />
              ) : (
                <StatusState kind="empty" title="空目录" />
              )
            ) : (
              <ul className="list" aria-label={`目录 ${path || '/'}`}>
                {path !== '' ? (
                  <li className="list-row">
                    <button
                      type="button"
                      className="list-row-main list-link btn-link"
                      onClick={() => setSearchParams({ path: parentPath(path) })}
                    >
                      <span className="ellipsis list-title">../</span>
                    </button>
                  </li>
                ) : null}
                {list.data!.data.entries.map((entry) => {
                  const fullPath = joinPath(path, entry.name)
                  return (
                    <EntryRow
                      key={fullPath}
                      name={entry.name}
                      type={entry.type}
                      fullPath={fullPath}
                      onOpen={() => openEntry(entry, fullPath)}
                    />
                  )
                })}
              </ul>
            )}
          </>
        )}
      </section>

      {file != null ? (
        <section className="panel" aria-label={`文件预览 ${file}`}>
          <h2 className="panel-title ellipsis">{basename(file)}</h2>
          {content.isPending ? (
            <StatusState kind="loading" title="正在加载文件…" />
          ) : content.isError ? (
            <QueryErrorState error={content.error} onRetry={() => content.refetch()} />
          ) : content.data!.data.binary ? (
            <StatusState
              kind="empty"
              title="二进制文件不可预览"
              description={`${basename(file)}（${content.data!.data.size} 字节）是二进制文件，本切片不提供预览。`}
            />
          ) : (
            <>
              <pre className="raw-json">{content.data!.data.text}</pre>
              <div className="state-actions">
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => setSearchParams(searching ? { path, q } : { path })}
                >
                  关闭预览
                </button>
              </div>
            </>
          )}
        </section>
      ) : null}
    </>
  )
}

export function FilesPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <FilesBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
