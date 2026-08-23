// 工作区浏览区（侧栏 regionArea 的内容），取自 @deepseek-ai/dsh-client-
// ui-workspace 的 WorkspaceBrowser (packages/client/ui-workspace/src/client/
// WorkspaceBrowser.tsx, MIT License)：段头（标题 + 搜索胶囊 + ＋动作）+
// 滚动的工作区分组树（工作区 34px 行 / 会话 32px 行，hover 交换纯 CSS）。
// 数据接 cockpit 的 file roots + herdr 会话（经 GroupChatPage 组装）；
// 未搬：拖拽排序、重命名、hover 卡、远程搜索（改为本地过滤）。
// Cockpit 4.0: 添加团队区（仅当配置 Team Hub 时显示）。
import { useEffect, useMemo, useRef, useState } from 'react'
import type { SessionRow } from '../group-chat/model'
import type { TeamBinding, TeamSessionCandidate, TeamTopic } from '../team/model'
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
  // Cockpit 4.0: 团队区
  teamEnabled?: boolean
  teamLoggedIn?: boolean
  teamUsername?: string | null
  teamIsAdmin?: boolean
  teamTopics?: TeamTopic[]
  teamBindings?: TeamBinding[]
  teamSessions?: TeamSessionCandidate[]
  teamActiveTopic?: string | null
  onTeamLogin?: (username: string, password: string) => Promise<void>
  onTeamRegister?: (input: {
    username: string
    displayName: string
    password: string
    inviteCode: string
  }) => Promise<void>
  onTeamLogout?: () => Promise<void>
  onTeamChangePassword?: (newPassword: string) => Promise<void>
  onTeamJoin?: (projectSlug: string, mentionHandle: string) => Promise<void>
  onTeamBindSession?: (projectSlug: string, sessionName: string) => Promise<void>
  onTeamSelectTopic?: (projectSlug: string) => void
  onOpenTeamAdmin?: () => void
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
        props.onOpen()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
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
  teamEnabled = false,
  teamLoggedIn = false,
  teamUsername = null,
  teamIsAdmin = false,
  teamTopics = [],
  teamBindings = [],
  teamSessions = [],
  teamActiveTopic = null,
  onTeamLogin,
  onTeamRegister,
  onTeamLogout,
  onTeamChangePassword,
  onTeamJoin,
  onTeamBindSession,
  onTeamSelectTopic,
  onOpenTeamAdmin,
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

                {/* Cockpit 4.0: 团队区（仅当配置 Team Hub 时显示） */}
                {teamEnabled && (
                  <TeamZoneSection
                    loggedIn={teamLoggedIn}
                    username={teamUsername}
                    isAdmin={teamIsAdmin}
                    topics={teamTopics}
                    bindings={teamBindings}
                    sessionCandidates={teamSessions}
                    activeTopic={teamActiveTopic}
                    onLogin={onTeamLogin || (async () => {})}
                    onRegister={onTeamRegister || (async () => {})}
                    onLogout={onTeamLogout || (async () => {})}
                    onChangePassword={onTeamChangePassword || (async () => {})}
                    onJoin={onTeamJoin || (async () => {})}
                    onBindSession={onTeamBindSession || (async () => {})}
                    onSelectTopic={onTeamSelectTopic || (() => {})}
                    onOpenTeamAdmin={onOpenTeamAdmin || (() => {})}
                  />
                )}
              </>
            )}
          </div>
          <div className={css.fade} />
        </div>
      </div>
    </div>
  )
}

// Cockpit 4.0: 团队区组件
function TeamZoneSection({
  loggedIn,
  username,
  isAdmin,
  topics,
  bindings,
  sessionCandidates,
  activeTopic,
  onLogin,
  onRegister,
  onLogout,
  onChangePassword,
  onJoin,
  onBindSession,
  onSelectTopic,
  onOpenTeamAdmin,
}: {
  loggedIn: boolean
  username: string | null
  isAdmin: boolean
  topics: TeamTopic[]
  bindings: TeamBinding[]
  sessionCandidates: TeamSessionCandidate[]
  activeTopic: string | null
  onLogin: (username: string, password: string) => Promise<void>
  onRegister: (input: {
    username: string
    displayName: string
    password: string
    inviteCode: string
  }) => Promise<void>
  onLogout: () => Promise<void>
  onChangePassword: (newPassword: string) => Promise<void>
  onJoin: (projectSlug: string, mentionHandle: string) => Promise<void>
  onBindSession: (projectSlug: string, sessionName: string) => Promise<void>
  onSelectTopic: (projectSlug: string) => void
  onOpenTeamAdmin: () => void
}) {
  const linkedInvite = useMemo(() => {
    const hash = window.location.hash
    const queryIndex = hash.indexOf('?')
    const params = new URLSearchParams(
      queryIndex >= 0 ? hash.slice(queryIndex + 1) : window.location.search,
    )
    const inviteCode = params.get('team_invite')?.trim() ?? ''
    const projectSlug = params.get('team_project')?.trim() ?? ''
    return inviteCode && projectSlug ? { inviteCode, projectSlug } : null
  }, [])
  const [authMode, setAuthMode] = useState<'idle' | 'login' | 'register'>(
    linkedInvite ? 'register' : 'idle',
  )
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [loginLoading, setLoginLoading] = useState(false)
  const [loginError, setLoginError] = useState<string | null>(null)
  const [registerUsername, setRegisterUsername] = useState('')
  const [registerDisplayName, setRegisterDisplayName] = useState('')
  const [registerInvite, setRegisterInvite] = useState(linkedInvite?.inviteCode ?? '')
  const [registerPassword, setRegisterPassword] = useState('')
  const [registerConfirm, setRegisterConfirm] = useState('')
  const [registerLoading, setRegisterLoading] = useState(false)
  const [registerError, setRegisterError] = useState<string | null>(null)
  const [registrationNotice, setRegistrationNotice] = useState<string | null>(null)
  const [bindingTopic, setBindingTopic] = useState<string | null>(null)
  const [changingPassword, setChangingPassword] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordLoading, setPasswordLoading] = useState(false)
  const [passwordError, setPasswordError] = useState<string | null>(null)
  const [passwordNotice, setPasswordNotice] = useState<string | null>(null)
  const [joiningTopic, setJoiningTopic] = useState<string | null>(null)
  const [joinHandle, setJoinHandle] = useState('')
  const [joinLoading, setJoinLoading] = useState(false)
  const [joinError, setJoinError] = useState<string | null>(null)

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!loginUsername.trim() || !loginPassword) return

    setLoginLoading(true)
    setLoginError(null)
    try {
      await onLogin(loginUsername.trim(), loginPassword)
      setAuthMode('idle')
      setLoginUsername('')
      setLoginPassword('')
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoginLoading(false)
    }
  }

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault()
    const username = registerUsername.trim()
    const displayName = registerDisplayName.trim()
    const inviteCode = registerInvite.trim()
    if (!inviteCode) {
      setRegisterError('请填写邀请码')
      return
    }
    if (!username || !displayName || !registerPassword) {
      setRegisterError('请完整填写账号、显示名和密码')
      return
    }
    if (registerPassword !== registerConfirm) {
      setRegisterError('两次输入的密码不一致')
      return
    }
    const passwordBytes = new TextEncoder().encode(registerPassword).length
    if (passwordBytes < 12 || passwordBytes > 256) {
      setRegisterError('密码必须是 12–256 个 UTF-8 字节')
      return
    }
    setRegisterLoading(true)
    setRegisterError(null)
    try {
      await onRegister({ username, displayName, password: registerPassword, inviteCode })
      setRegistrationNotice(
        linkedInvite
          ? `账号 ${username} 已提交加入 ${linkedInvite.projectSlug}；管理员一次批准后即可登录。`
          : `账号 ${username} 已提交，当前为待批准；管理员批准后即可登录。`,
      )
      setRegisterUsername('')
      setRegisterDisplayName('')
      setRegisterInvite('')
      setRegisterPassword('')
      setRegisterConfirm('')
      setAuthMode('idle')
    } catch (err) {
      setRegisterError(err instanceof Error ? err.message : String(err))
    } finally {
      setRegisterLoading(false)
    }
  }

  const startJoin = (projectSlug: string) => {
    const suggested = (username ?? '')
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 128)
    setJoiningTopic(projectSlug)
    setJoinHandle(suggested || 'human')
    setJoinError(null)
  }

  const handleJoin = async (projectSlug: string) => {
    const mentionHandle = joinHandle.trim()
    if (!mentionHandle) {
      setJoinError('请填写项目内 @花名')
      return
    }
    setJoinLoading(true)
    setJoinError(null)
    try {
      await onJoin(projectSlug, mentionHandle)
      setJoiningTopic(null)
      setJoinHandle('')
    } catch (err) {
      setJoinError(err instanceof Error ? err.message : String(err))
    } finally {
      setJoinLoading(false)
    }
  }

  const handleBind = async (projectSlug: string, sessionName: string) => {
    try {
      await onBindSession(projectSlug, sessionName)
      setBindingTopic(null)
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err))
    }
  }

  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault()
    if (newPassword !== confirmPassword) {
      setPasswordError('两次输入的密码不一致')
      return
    }
    const passwordBytes = new TextEncoder().encode(newPassword).length
    if (passwordBytes < 12 || passwordBytes > 256) {
      setPasswordError('密码必须是 12–256 个 UTF-8 字节')
      return
    }
    setPasswordLoading(true)
    setPasswordError(null)
    setPasswordNotice(null)
    try {
      await onChangePassword(newPassword)
      setNewPassword('')
      setConfirmPassword('')
      setChangingPassword(false)
      setPasswordNotice('密码已修改；现有登录保持有效。')
    } catch (err) {
      setPasswordError(err instanceof Error ? err.message : String(err))
    } finally {
      setPasswordLoading(false)
    }
  }

  const compactInputStyle = {
    width: '100%',
    boxSizing: 'border-box',
    padding: '8px 10px',
    background: 'var(--dsw-alias-bg-base)',
    border: '1px solid var(--dsw-alias-border-l1)',
    borderRadius: '6px',
    color: 'var(--dsw-alias-label-primary)',
    fontSize: '13px',
  } as const
  const compactButtonStyle = {
    padding: '8px',
    background: 'var(--dsw-alias-bg-l2)',
    border: '1px solid var(--dsw-alias-border-l1)',
    borderRadius: '6px',
    color: 'var(--dsw-alias-label-primary)',
    cursor: 'pointer',
    fontSize: '13px',
  } as const

  if (!loggedIn) {
    if (authMode === 'idle') {
      return (
        <div className={css.groupSection} style={{ marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
          <div style={{ padding: '0 12px', marginBottom: '8px', fontSize: '12px', fontWeight: 500, color: 'var(--dsw-alias-label-secondary)' }}>
            团队
          </div>
          {registrationNotice && (
            <div style={{ margin: '0 8px 8px', color: 'var(--dsw-alias-state-success-primary)', fontSize: '12px' }}>
              {registrationNotice}
            </div>
          )}
          <div style={{ display: 'flex', gap: '6px', padding: '0 8px' }}>
            <button
              type="button"
              style={{ flex: 1, padding: '8px', background: 'var(--dsw-alias-bg-l2)', border: '1px solid var(--dsw-alias-border-l1)', borderRadius: '6px', color: 'var(--dsw-alias-label-primary)', cursor: 'pointer', fontSize: '13px' }}
              onClick={() => setAuthMode('login')}
            >
              登录团队账号
            </button>
            <button
              type="button"
              style={{ flex: 1, padding: '8px', background: 'var(--dsw-alias-bg-l2)', border: '1px solid var(--dsw-alias-border-l1)', borderRadius: '6px', color: 'var(--dsw-alias-label-primary)', cursor: 'pointer', fontSize: '13px' }}
              onClick={() => setAuthMode('register')}
            >
              邀请码注册
            </button>
          </div>
        </div>
      )
    }

    if (authMode === 'register') {
      return (
        <div className={css.groupSection} style={{ marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
          <div style={{ padding: '0 12px', marginBottom: '8px', fontSize: '12px', fontWeight: 500, color: 'var(--dsw-alias-label-secondary)' }}>
            注册团队账号
          </div>
          <form style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '0 8px' }} onSubmit={handleRegister}>
            {linkedInvite && (
              <div style={{ color: 'var(--dsw-alias-state-success-primary)', fontSize: '12px' }}>
                受邀加入 {linkedInvite.projectSlug}
              </div>
            )}
            <input aria-label="注册用户名" placeholder="用户名" value={registerUsername} onChange={(e) => setRegisterUsername(e.target.value)} disabled={registerLoading} autoComplete="username" style={compactInputStyle} />
            <input aria-label="注册显示名" placeholder="显示名" value={registerDisplayName} onChange={(e) => setRegisterDisplayName(e.target.value)} disabled={registerLoading} autoComplete="name" style={compactInputStyle} />
            <input aria-label="团队邀请码" placeholder="一次性邀请码" value={registerInvite} onChange={(e) => setRegisterInvite(e.target.value)} disabled={registerLoading} readOnly={!!linkedInvite} autoComplete="off" style={compactInputStyle} />
            <input aria-label="注册密码" type="password" placeholder="密码（至少 12 字节）" value={registerPassword} onChange={(e) => setRegisterPassword(e.target.value)} disabled={registerLoading} autoComplete="new-password" style={compactInputStyle} />
            <input aria-label="确认注册密码" type="password" placeholder="确认密码" value={registerConfirm} onChange={(e) => setRegisterConfirm(e.target.value)} disabled={registerLoading} autoComplete="new-password" style={compactInputStyle} />
            {registerError && <div style={{ color: 'var(--dsw-alias-state-error-primary)', fontSize: '12px' }}>{registerError}</div>}
            <div style={{ display: 'flex', gap: '6px' }}>
              <button type="submit" disabled={registerLoading} style={{ ...compactButtonStyle, flex: 1 }}>
                {registerLoading ? '提交中…' : '提交注册申请'}
              </button>
              <button type="button" disabled={registerLoading} onClick={() => { setAuthMode('idle'); setRegisterError(null) }} style={{ ...compactButtonStyle, flex: 1 }}>
                取消
              </button>
            </div>
          </form>
        </div>
      )
    }

    return (
      <div className={css.groupSection} style={{ marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
        <div style={{ padding: '0 12px', marginBottom: '8px', fontSize: '12px', fontWeight: 500, color: 'var(--dsw-alias-label-secondary)' }}>
          团队登录
        </div>
        <form
          style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '0 8px' }}
          onSubmit={handleLogin}
        >
          <input
            type="text"
            placeholder="用户名"
            value={loginUsername}
            onChange={(e) => setLoginUsername(e.target.value)}
            disabled={loginLoading}
            autoComplete="username"
            style={{
              padding: '8px 10px',
              background: 'var(--dsw-alias-bg-base)',
              border: '1px solid var(--dsw-alias-border-l1)',
              borderRadius: '6px',
              color: 'var(--dsw-alias-label-primary)',
              fontSize: '13px',
            }}
          />
          <input
            type="password"
            placeholder="密码"
            value={loginPassword}
            onChange={(e) => setLoginPassword(e.target.value)}
            disabled={loginLoading}
            autoComplete="current-password"
            style={{
              padding: '8px 10px',
              background: 'var(--dsw-alias-bg-base)',
              border: '1px solid var(--dsw-alias-border-l1)',
              borderRadius: '6px',
              color: 'var(--dsw-alias-label-primary)',
              fontSize: '13px',
            }}
          />
          {loginError && (
            <div style={{ color: 'var(--dsw-alias-state-error-primary)', fontSize: '12px', padding: '4px' }}>
              {loginError}
            </div>
          )}
          <div style={{ display: 'flex', gap: '6px' }}>
            <button
              type="submit"
              disabled={loginLoading}
              style={{
                flex: 1,
                padding: '8px',
                background: 'var(--dsw-alias-bg-l2)',
                border: '1px solid var(--dsw-alias-border-l1)',
                borderRadius: '6px',
                color: 'var(--dsw-alias-label-primary)',
                cursor: loginLoading ? 'not-allowed' : 'pointer',
                fontSize: '13px',
                opacity: loginLoading ? 0.5 : 1,
              }}
            >
              {loginLoading ? '登录中…' : '登录'}
            </button>
            <button
              type="button"
              onClick={() => {
                setAuthMode('idle')
                setLoginError(null)
              }}
              disabled={loginLoading}
              style={{
                flex: 1,
                padding: '8px',
                background: 'var(--dsw-alias-bg-l2)',
                border: '1px solid var(--dsw-alias-border-l1)',
                borderRadius: '6px',
                color: 'var(--dsw-alias-label-primary)',
                cursor: loginLoading ? 'not-allowed' : 'pointer',
                fontSize: '13px',
                opacity: loginLoading ? 0.5 : 1,
              }}
            >
              取消
            </button>
          </div>
        </form>
      </div>
    )
  }

  return (
    <div className={css.groupSection} style={{ marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
      <div style={{ padding: '0 12px', marginBottom: '8px', fontSize: '12px', fontWeight: 500, color: 'var(--dsw-alias-label-secondary)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span>团队 ({username})</span>
        <button
          type="button"
          onClick={() => void onLogout()}
          title="退出登录"
          style={{ background: 'none', border: 'none', color: 'var(--dsw-alias-label-tertiary)', cursor: 'pointer', fontSize: '14px', padding: '0 4px' }}
        >
          ⎋
        </button>
      </div>

      <button
        type="button"
        onClick={() => { setChangingPassword((value) => !value); setPasswordError(null); setPasswordNotice(null) }}
        title="修改团队登录密码"
        style={{ ...compactButtonStyle, display: 'block', width: 'calc(100% - 16px)', margin: '0 8px 8px' }}
      >
        {changingPassword ? '收起修改密码' : '修改登录密码'}
      </button>

      {changingPassword && (
        <form style={{ display: 'flex', flexDirection: 'column', gap: '6px', margin: '0 8px 8px' }} onSubmit={handlePasswordChange}>
          <input aria-label="新团队密码" type="password" placeholder="新密码（至少 12 字节）" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} disabled={passwordLoading} autoComplete="new-password" style={compactInputStyle} />
          <input aria-label="确认新团队密码" type="password" placeholder="再次输入新密码" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} disabled={passwordLoading} autoComplete="new-password" style={compactInputStyle} />
          {passwordError && <div style={{ color: 'var(--dsw-alias-state-error-primary)', fontSize: '12px' }}>{passwordError}</div>}
          <div style={{ display: 'flex', gap: '6px' }}>
            <button type="submit" disabled={passwordLoading} style={{ ...compactButtonStyle, flex: 1 }}>{passwordLoading ? '修改中…' : '保存新密码'}</button>
            <button type="button" disabled={passwordLoading} onClick={() => { setChangingPassword(false); setPasswordError(null); setNewPassword(''); setConfirmPassword('') }} style={{ ...compactButtonStyle, flex: 1 }}>取消</button>
          </div>
        </form>
      )}
      {passwordNotice && <div style={{ margin: '0 8px 8px', color: 'var(--dsw-alias-state-success-primary)', fontSize: '12px' }}>{passwordNotice}</div>}

      {topics.length === 0 && (
        <div style={{ padding: '8px 12px', fontSize: '12px', color: 'var(--dsw-alias-label-tertiary)', textAlign: 'center' }}>
          还没有 topic。{isAdmin ? '到团队管理页新建。' : '请管理员在团队管理页新建。'}
        </div>
      )}

      {topics.map((topic) => {
        const binding = bindings.find((b) => b.project_slug === topic.slug)
        const selectableCandidates = sessionCandidates.filter((candidate) => (
          candidate.ready
          && (!binding?.projectRef || candidate.projectRef === binding.projectRef)
        ))
        const membershipStatus = topic.membership === undefined
          ? 'active'
          : topic.membership?.status ?? ''
        const isActive = membershipStatus === 'active'
        const isInvited = membershipStatus === 'invited'
        const canRequestJoin = !membershipStatus || membershipStatus === 'removed'
        const isBound = isActive && !!binding
        const bindingIsLive = !!binding && binding.active !== false
        const isBinding = bindingTopic === topic.slug
        const isJoining = joiningTopic === topic.slug

        return (
          <div key={topic.slug} style={{ position: 'relative', marginBottom: '2px' }}>
            <button
              type="button"
              className={cx(css.sessionRow, activeTopic === topic.slug && css.selected)}
              onClick={() => isBound && onSelectTopic(topic.slug)}
              disabled={!isBound}
              title={
                isBound
                  ? `打开 ${topic.name}（绑定到 ${binding.session}${bindingIsLive ? '' : '，已停止'}）`
                  : isActive
                    ? `${topic.name}（需要先绑定本机 Session）`
                    : isInvited
                      ? `${topic.name}（加入申请等待审批）`
                      : `${topic.name}（尚未加入）`
              }
              style={{
                opacity: isBound ? 1 : 0.6,
                cursor: isBound ? 'pointer' : 'not-allowed',
              }}
            >
              <span className={css.slot}>
                <span className={css.statusDot} data-status={isBound ? 'active' : 'stopped'} />
              </span>
              <span className={css.title}>{topic.name}</span>
              <span className={css.meta}>
                {isBound
                  ? `→ ${binding.session}${bindingIsLive ? '' : '（已停止）'}`
                  : isActive ? '未绑定' : isInvited ? '等待审批' : '未加入'}
              </span>
            </button>

            {isActive && !isBinding && (
              <button
                type="button"
                onClick={() => setBindingTopic(topic.slug)}
                title={isBound ? '更换本机 Session' : '绑定本机 Session'}
                style={{
                  position: 'absolute',
                  right: '8px',
                  top: '50%',
                  transform: 'translateY(-50%)',
                  background: 'var(--dsw-alias-state-business-primary)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '4px',
                  padding: '4px 10px',
                  fontSize: '11px',
                  cursor: 'pointer',
                  fontWeight: 500,
                }}
              >
                {isBound ? '改绑' : '绑定'}
              </button>
            )}

            {canRequestJoin && !isJoining && (
              <button
                type="button"
                onClick={() => startJoin(topic.slug)}
                title={`申请加入 ${topic.name}`}
                style={{ position: 'absolute', right: '8px', top: '50%', transform: 'translateY(-50%)', background: 'var(--dsw-alias-state-business-primary)', color: 'white', border: 'none', borderRadius: '4px', padding: '4px 10px', fontSize: '11px', cursor: 'pointer', fontWeight: 500 }}
              >
                申请加入
              </button>
            )}

            {isJoining && (
              <form
                style={{ background: 'var(--dsw-alias-bg-base)', border: '1px solid var(--dsw-alias-border-l1)', borderRadius: '6px', padding: '8px', margin: '4px 8px 8px' }}
                onSubmit={(e) => { e.preventDefault(); void handleJoin(topic.slug) }}
              >
                <div style={{ fontSize: '12px', color: 'var(--dsw-alias-label-secondary)', marginBottom: '6px' }}>
                  项目内 @花名
                </div>
                <input
                  aria-label={`${topic.name} @花名`}
                  value={joinHandle}
                  onChange={(e) => setJoinHandle(e.target.value)}
                  disabled={joinLoading}
                  style={{ ...compactInputStyle, marginBottom: '6px' }}
                />
                {joinError && <div style={{ color: 'var(--dsw-alias-state-error-primary)', fontSize: '12px', marginBottom: '6px' }}>{joinError}</div>}
                <div style={{ display: 'flex', gap: '6px' }}>
                  <button type="submit" disabled={joinLoading} style={{ ...compactButtonStyle, flex: 1 }}>
                    {joinLoading ? '提交中…' : '提交申请'}
                  </button>
                  <button type="button" disabled={joinLoading} onClick={() => { setJoiningTopic(null); setJoinError(null) }} style={{ ...compactButtonStyle, flex: 1 }}>
                    取消
                  </button>
                </div>
              </form>
            )}

            {isBinding && (
              <div style={{
                background: 'var(--dsw-alias-bg-base)',
                border: '1px solid var(--dsw-alias-border-l1)',
                borderRadius: '6px',
                padding: '8px',
                margin: '4px 8px 8px',
              }}>
                <div style={{ fontSize: '12px', color: 'var(--dsw-alias-label-secondary)', marginBottom: '6px' }}>
                  选择本机 Session：
                </div>
                {selectableCandidates.length === 0 && (
                  <div style={{ padding: '6px 2px', fontSize: '12px', color: 'var(--dsw-alias-label-tertiary)' }}>
                    没有与该项目匹配且负责人可用的 Session
                  </div>
                )}
                {selectableCandidates.map((sess) => (
                  <button
                    key={sess.name}
                    type="button"
                    title={sess.reason ?? sess.label}
                    onClick={() => handleBind(topic.slug, sess.name)}
                    style={{
                      display: 'block',
                      width: '100%',
                      padding: '6px 10px',
                      background: 'var(--dsw-alias-bg-l2)',
                      border: '1px solid var(--dsw-alias-border-l1)',
                      borderRadius: '4px',
                      color: 'var(--dsw-alias-label-primary)',
                      cursor: 'pointer',
                      fontSize: '12px',
                      textAlign: 'left',
                      marginBottom: '4px',
                    }}
                  >
                    {sess.label}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={() => setBindingTopic(null)}
                  style={{
                    display: 'block',
                    width: '100%',
                    padding: '6px 10px',
                    background: 'var(--dsw-alias-bg-base)',
                    border: '1px solid var(--dsw-alias-border-l1)',
                    borderRadius: '4px',
                    color: 'var(--dsw-alias-label-tertiary)',
                    cursor: 'pointer',
                    fontSize: '12px',
                    marginTop: '4px',
                  }}
                >
                  取消
                </button>
              </div>
            )}
          </div>
        )
      })}

      {isAdmin && (
        <button
          type="button"
          onClick={() => onOpenTeamAdmin()}
          style={{
            display: 'block',
            width: 'calc(100% - 16px)',
            margin: '8px 8px 0',
            padding: '6px 10px',
            background: 'none',
            border: 'none',
            borderTop: '1px solid var(--dsw-alias-border-l1)',
            color: 'var(--dsw-alias-label-tertiary)',
            cursor: 'pointer',
            fontSize: '12px',
            textAlign: 'left',
          }}
        >
          团队管理（账号 / 成员审批）→
        </button>
      )}
    </div>
  )
}
