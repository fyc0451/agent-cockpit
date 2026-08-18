// 工作区浏览区（侧栏 regionArea 的内容），取自 @deepseek-ai/dsh-client-
// ui-workspace 的 WorkspaceBrowser (packages/client/ui-workspace/src/client/
// WorkspaceBrowser.tsx, MIT License)：段头（标题 + 搜索胶囊 + ＋动作）+
// 滚动的工作区分组树（工作区 34px 行 / 会话 32px 行，hover 交换纯 CSS）。
// 数据接 cockpit 的 file roots + herdr 会话（经 GroupChatPage 组装）；
// 未搬：拖拽排序、重命名、hover 卡、远程搜索（改为本地过滤）。
import { useEffect, useMemo, useRef, useState } from 'react'
import type { SessionRow } from '../group-chat/model'
import { useAppFrame } from './AppFrame'
import { cx } from './cx'
import {
  IconCloseFill14,
  IconFolderOpenOutline16,
  IconNewChatOutline16,
  IconProjectAddOutline16,
  IconSearchOutline16,
  IconStopFill16,
  IconTriangleRightFill14,
} from './icons'
import css from './WorkspaceBrowser.module.css'

/** 折叠态每组可见会话数；超出走本地 overflow 控制。 */
const COLLAPSED_SESSION_LIMIT = 5

export interface WorkspaceGroup {
  id: string // 账本工作区 id；未分组用空串
  root: string // 工作区目录
  label: string // basename
  removable: boolean // 已登记工作区都可移除
  rows: SessionRow[] // 该工作区下的会话
}

export interface WorkspaceBrowserProps {
  groups: WorkspaceGroup[]
  ungrouped: SessionRow[] // 不属于任何工作区的会话
  activeSession: string | null
  loading: boolean
  /** 侧栏宽态（rail 态只渲染两个 36x36 图标钮）。 */
  wide: boolean
  onSelect: (session: string) => void
  onAddWorkspace: () => void
  onNewSession: (root: string) => void
  onRemoveWorkspace: (id: string) => void
  onStopSession: (session: string) => void
  onDeleteSession: (session: string) => void
  onOpenWorkspace: (id: string) => void
}

/** 工作区分组头行：folder 图标 hover 换展开箭头，尾部动作钮 hover 出现。 */
function ProjectRow(props: {
  label: string
  root: string
  open: boolean
  canCreate: boolean
  removable: boolean
  onToggle: () => void
  onOpen: () => void
  onNewSession: () => void
  onRemoveWorkspace: () => void
}) {
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: 键盘走下方 onKeyDown
    <div
      className={css.projectRow}
      role="button"
      tabIndex={0}
      aria-expanded={props.open}
      title={`${props.root}（点击打开）`}
      onClick={() => {
        props.onToggle()
        props.onOpen()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          props.onToggle()
          props.onOpen()
        }
      }}
    >
      <span className={css.slot}>
        <IconFolderOpenOutline16 className={css.folder} size={16} />
        <IconTriangleRightFill14 className={cx(css.arrow, props.open && css.arrowOpen)} size={14} />
      </span>
      <span className={css.projectText}>
        <span className={css.title}>{props.label}</span>
      </span>
      <span className={css.rowActions}>
        {props.canCreate && (
          <button
            type="button"
            className={css.iconButton}
            title={`在 ${props.label} 创建会话`}
            onClick={(e) => {
              e.stopPropagation()
              props.onNewSession()
            }}
          >
            <IconNewChatOutline16 size={16} />
          </button>
        )}
        {props.removable && (
          <button
            type="button"
            className={cx(css.iconButton, css.iconDanger)}
            title={`移除工作区 ${props.label}（不影响目录本身）`}
            onClick={(e) => {
              e.stopPropagation()
              props.onRemoveWorkspace()
            }}
          >
            <IconCloseFill14 size={14} />
          </button>
        )}
      </span>
    </div>
  )
}

/** 渲染工作区浏览区（见模块注释）。 */
export function WorkspaceBrowser({
  groups,
  ungrouped,
  activeSession,
  loading,
  wide,
  onSelect,
  onAddWorkspace,
  onNewSession,
  onRemoveWorkspace,
  onStopSession,
  onDeleteSession,
  onOpenWorkspace,
}: WorkspaceBrowserProps) {
  const { toggleSidebar } = useAppFrame()
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  // 用户手动折叠的分组（root 键）。未分组用 '' 键，默认收起，不跟已入账群聊抢视线。
  const [closedGroups, setClosedGroups] = useState<Set<string>>(() => new Set(['']))
  // overflow 展开（显示全部会话）的分组。
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(() => new Set())
  const searchInput = useRef<HTMLInputElement | null>(null)

  // 搜索展开时聚焦输入框。
  useEffect(() => {
    if (!wide || !searchOpen) return
    requestAnimationFrame(() => searchInput.current?.focus())
  }, [wide, searchOpen])

  const toggleGroup = (key: string) => {
    setClosedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }
  const toggleOverflow = (key: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const q = query.trim().toLowerCase()
  const searching = q.length > 0
  // 搜索索引：每会话带上所属工作区（null = 未分组）。
  const allRows = useMemo(() => {
    const out: Array<{ row: SessionRow; group: WorkspaceGroup | null }> = []
    for (const g of groups) for (const r of g.rows) out.push({ row: r, group: g })
    for (const r of ungrouped) out.push({ row: r, group: null })
    return out
  }, [groups, ungrouped])
  const matched = useMemo(
    () => (searching ? allRows.filter((e) => e.row.name.toLowerCase().includes(q)) : null),
    [searching, q, allRows],
  )

  // rail 态：两个 36x36 图标钮（搜索 = 展开侧栏，＋ = 添加工作区）。
  if (!wide) {
    return (
      <div className={cx(css.root, css.rail)}>
        <div className={css.sectionHeader}>
          <button
            type="button"
            className={css.searchButton}
            aria-label="搜索会话"
            title="搜索会话"
            onClick={() => { toggleSidebar() }}
          >
            <IconSearchOutline16 size={16} />
          </button>
          <div className={css.headerActions}>
            <button
              type="button"
              className={css.iconButton}
              aria-label="添加工作区"
              title="添加工作区"
              onClick={() => { onAddWorkspace() }}
            >
              <IconProjectAddOutline16 size={16} />
            </button>
          </div>
        </div>
      </div>
    )
  }

  const renderGroup = (key: string, label: string, rows: SessionRow[], opts: { id?: string; root?: string; canCreate: boolean; removable: boolean }) => {
    const open = !closedGroups.has(key)
    const expanded = expandedGroups.has(key)
    const shown = open ? (expanded ? rows : rows.slice(0, COLLAPSED_SESSION_LIMIT)) : []
    return (
      <div key={key} className={css.groupSection}>
        <ProjectRow
          label={label}
          root={opts.root ?? label}
          open={open}
          canCreate={opts.canCreate}
          removable={opts.removable}
          onToggle={() => { toggleGroup(key) }}
          onOpen={() => { if (opts.id) onOpenWorkspace(opts.id) }}
          onNewSession={() => { if (opts.root) onNewSession(opts.root) }}
          onRemoveWorkspace={() => { if (opts.id) onRemoveWorkspace(opts.id) }}
        />
        {open && (
          <>
            {shown.map((row) => (
              <div
                key={row.name}
                role="treeitem"
                aria-selected={row.name === activeSession}
                className={cx(css.sessionRow, row.name === activeSession && css.selected)}
                title={row.name}
              >
                <button
                  type="button"
                  className={css.sessionMain}
                  onClick={() => { onSelect(row.name) }}
                >
                  <span className={css.slot}>
                    <span className={css.statusDot} data-status={row.status} />
                  </span>
                  <span className={css.title}>{row.name}</span>
                  <span className={css.meta}>
                    {row.status === 'stopped' ? '已停止' : `${row.memberCount} 人`}
                  </span>
                </button>
                <span className={css.rowActions}>
                  {row.status !== 'stopped' && (
                    <button
                      type="button"
                      className={css.iconButton}
                      title={`停止会话 ${row.name}`}
                      onClick={(e) => {
                        e.stopPropagation()
                        onStopSession(row.name)
                      }}
                    >
                      <IconStopFill16 size={16} />
                    </button>
                  )}
                  <button
                    type="button"
                    className={cx(css.iconButton, css.iconDanger)}
                    title={`删除会话 ${row.name}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      onDeleteSession(row.name)
                    }}
                  >
                    <IconCloseFill14 size={14} />
                  </button>
                </span>
              </div>
            ))}
            {rows.length > COLLAPSED_SESSION_LIMIT && (
              <button
                type="button"
                className={css.sessionOverflowButton}
                aria-expanded={expanded}
                onClick={() => { toggleOverflow(key) }}
              >
                {expanded ? '收起' : `还有 ${rows.length - COLLAPSED_SESSION_LIMIT} 个会话`}
              </button>
            )}
          </>
        )}
      </div>
    )
  }

  return (
    <div className={css.root}>
      <div className={css.sectionHeader}>
        <span className={cx(css.sectionLabel, searchOpen && css.sectionLabelHidden)}>工作区</span>
        <div className={cx(css.searchSlot, searchOpen && css.searchSlotExpanded)}>
          <div className={cx(css.search, searchOpen && css.searchExpanded)}>
            <button
              type="button"
              className={css.searchButton}
              aria-label="搜索会话"
              aria-expanded={searchOpen}
              onClick={() => { setSearchOpen(true) }}
            >
              <IconSearchOutline16 size={searchOpen ? 11 : 14} />
            </button>
            <input
              ref={searchInput}
              className={css.searchInput}
              value={query}
              placeholder="搜索会话"
              tabIndex={searchOpen ? 0 : -1}
              onChange={(e) => { setQuery(e.target.value) }}
              onKeyDown={(e) => {
                if (e.key !== 'Escape') return
                setQuery('')
                setSearchOpen(false)
              }}
            />
            {searchOpen && query !== '' && (
              <button
                type="button"
                className={css.clearButton}
                aria-label="清空搜索"
                onClick={() => {
                  setQuery('')
                  searchInput.current?.focus()
                }}
              >
                <IconCloseFill14 size={12} />
              </button>
            )}
          </div>
        </div>
        <div className={cx(css.headerActions, searchOpen && css.headerActionsHidden)}>
          <button
            type="button"
            className={css.iconButton}
            aria-label="添加工作区"
            title="添加工作区"
            onClick={() => { onAddWorkspace() }}
          >
            <IconProjectAddOutline16 size={16} />
          </button>
        </div>
      </div>

      <div className={css.listArea}>
        <div className={css.treeBody}>
          <div className={cx(css.list, searching && css.searchTree)}>
            {searching ? (
              matched !== null && matched.length === 0 ? (
                <div className={css.searchStatus}>没有匹配「{query.trim()}」的会话</div>
              ) : (
                matched?.map(({ row, group }) => (
                  <button
                    key={row.name}
                    type="button"
                    role="treeitem"
                    aria-selected={row.name === activeSession}
                    className={cx(css.searchResultRow, row.name === activeSession && css.selected)}
                    onClick={() => { onSelect(row.name) }}
                  >
                    <span className={css.searchResultHeading}>
                      <span className={css.statusDot} data-status={row.status} />
                      <span className={css.searchResultTitle}>{row.name}</span>
                    </span>
                    <span className={css.searchResultMeta}>
                      <span className={css.searchResultWorkspace}>{group ? group.label : '未分组'}</span>
                      <span className={css.searchResultSnippet}>{row.memberCount} 名成员</span>
                    </span>
                  </button>
                ))
              )
            ) : (
              <>
                {groups.length === 0 && ungrouped.length === 0 && (
                  <div className={css.empty}>
                    {loading ? '会话加载中…' : '还没有工作区。点右上 ＋ 添加一个工作目录。'}
                  </div>
                )}
                {groups.map((g) => renderGroup(g.id || g.root, g.label, g.rows, { id: g.id, root: g.root, canCreate: true, removable: g.removable }))}
                {ungrouped.length > 0 && renderGroup('', '未分组', ungrouped, { canCreate: false, removable: false })}
              </>
            )}
          </div>
          <div className={css.fade} />
        </div>
      </div>
    </div>
  )
}
