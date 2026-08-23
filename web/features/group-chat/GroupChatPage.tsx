// 群聊工作台主页（顶层路由 /chat）：DeepSeek Harness 三栏外壳
// （AppFrame：侧栏工作区浏览 | 中栏群聊瀑布流+输入卡 | details 栏
// 成员/文件 tab）。工作区/群聊归属读账本，不再画 file-roots。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { routePatterns, routes } from '../../app/routes'
import { SettingsPage } from '../../pages/SettingsPage'
import { TeamAdminPage } from '../../pages/TeamAdminPage'
import {
  deleteHerdrSession,
  fetchHerdrSessions,
  isAlreadyStoppedError,
  fetchHerdrSnapshot,
  fetchHerdrStatus,
  stopHerdrSession,
} from '../../api/legacyHerdr'
import {
  bindChatWorkspace,
  deleteChatWorkspace,
  fetchChatLedger,
  openChatWorkspace,
  type ChatBindCandidate,
} from '../../api/chatLedger'
import {
  applyMailStreamEvent,
  fetchSessionMail,
  preferLedgerMail,
  sendSessionMail,
  sessionMailStreamUrl,
  uploadChatFile,
  type SessionMailMessage,
} from '../../api/chatSession'
import { fetchTeamConfig } from '../../api/teamConfig'
import {
  teamAuthStatus,
  teamChangePassword,
  teamLogin,
  teamRegister,
  teamLogout,
  teamSessionBindings,
  teamBindSession,
  listTeamProjects,
  listTeamMembers,
  requestTeamJoin,
} from '../../api/teamAuth'
import { TeamTimeline } from '../team/TeamTimeline'
import type { TeamBinding, TeamSessionCandidate, TeamTopic } from '../team/model'
import { requireAuthenticated } from '../../api/auth'
import { ApiError } from '../../api/client'
import { AppFrame, useAppFrame } from '../shell/AppFrame'
import { SidebarRoot } from '../shell/SidebarRoot'
import { WorkspaceBrowser } from '../shell/WorkspaceBrowser'
import { AddWorkspaceModal } from './AddWorkspaceModal'
import { AgentInteractModal } from './AgentInteractModal'
import { AgentMailStatusBar } from './AgentMailStatusBar'
import { Composer } from './Composer'
import { DetailsPanel, type DetailsTab } from './DetailsPanel'
import { AgentIcon } from './AgentIcon'
import { FilePreview } from './FilePreview'
import { HerdrTerminalModal } from './HerdrTerminalModal'
import { NewSessionWizard } from './NewSessionWizard'
import { Waterfall, type ChatEntry } from './Waterfall'
import {
  appendAttachMarkup,
  avatarColor,
  buildSessionRows,
  canRecallEntry,
  groupByLedger,
  clearActiveSession,
  loadActiveSession,
  loadComposerDraft,
  loadLocalEntries,
  saveComposerDraft,
  nextSessionAfterRemoval,
  shouldFollowUrlSession,
  shouldRefreshMembersOnSelect,
  mailCoversLocalMe,
  isBusyMember,
  isMemberRosterEvent,
  hasBroadcastMention,
  isDirectMessageVisible,
  mailToEntries,
  membersOfSession,
  parseMentionTargets,
  typingEntries,
  recallNotice,
  rootBase,
  saveActiveSession,
  saveLocalEntries,
  shouldSeedMemberRoster,
  withLeader,
  type ChatDelivery,
  type ChatMember,
} from './model'
import './groupChat.css'

const POLL_MS = 10_000
export const TEAM_BINDINGS_REFRESH_MS = 5_000
const MAX_ENTRIES = 300

function restoreLocalEntries(session: string): ChatEntry[] {
  const out: ChatEntry[] = []
  for (const raw of loadLocalEntries(session)) {
    const id = typeof raw.id === 'string' ? raw.id : ''
    const ts = typeof raw.ts === 'number' ? raw.ts : 0
    if (!id) continue
    if (raw.kind === 'me' && typeof raw.text === 'string') {
      out.push({
        id,
        kind: 'me',
        text: raw.text,
        to: Array.isArray(raw.to) ? raw.to.filter((item): item is string => typeof item === 'string') : [],
        mailTo: Array.isArray(raw.mailTo)
          ? raw.mailTo.filter((item): item is string => typeof item === 'string')
          : [],
        ts,
        recalled: raw.recalled === true,
        delivery: raw.delivery === 'queue' || raw.delivery === 'interrupt' ? raw.delivery : undefined,
      })
      continue
    }
    if (
      (raw.kind === 'event' || raw.kind === 'error') &&
      typeof raw.text === 'string' &&
      !(raw.kind === 'event' && isMemberRosterEvent(raw.text))
    ) {
      out.push({ id, kind: raw.kind, text: raw.text, ts })
    }
  }
  return out
}

/**
 * 工具栏右侧的 details 快捷钮：已开且同 tab 再点关闭，异 tab 切换并
 * 确保打开。须渲染在 AppFrame 内（消费 useAppFrame）。
 */
function NarrowAwareBrowser(props: {
  groups: ReturnType<typeof groupByLedger>['groups']
  ungrouped: ReturnType<typeof groupByLedger>['ungrouped']
  activeSession: string | null
  loading: boolean
  wide: boolean
  onSelect: (session: string) => void
  onAddWorkspace: () => void
  onNewSession: (root: string) => void
  onRemoveWorkspace: (id: string) => void
  onStopSession: (session: string) => void
  onDeleteSession: (session: string) => void
  onOpenWorkspace: (id: string) => void
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
}) {
  const { narrow, sidebarCollapsed, toggleSidebar } = useAppFrame()
  const closeRail = () => {
    if (narrow && !sidebarCollapsed) toggleSidebar()
  }
  return (
    <WorkspaceBrowser
      {...props}
      onSelect={(session) => {
        props.onSelect(session)
        closeRail()
      }}
      onOpenWorkspace={(id) => {
        props.onOpenWorkspace(id)
        closeRail()
      }}
    />
  )
}

function DetailsTabButton(props: {
  tab: DetailsTab
  current: DetailsTab
  onSelect: (tab: DetailsTab) => void
  children: ReactNode
}) {
  const { detailsOpen, toggleDetails } = useAppFrame()
  const active = detailsOpen && props.current === props.tab
  return (
    <button
      type="button"
      className={`gc-tab-btn${active ? ' is-on' : ''}`}
      onClick={() => {
        if (detailsOpen && props.current === props.tab) {
          toggleDetails()
          return
        }
        props.onSelect(props.tab)
        if (!detailsOpen) toggleDetails()
      }}
    >
      {props.children}
    </button>
  )
}

export function GroupChatPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const isSettings = location.pathname === routePatterns.settings
  const isTeamAdmin = location.pathname === routePatterns.team
  const [searchParams, setSearchParams] = useSearchParams()
  const urlSession = searchParams.get('session')

  // ---------- 数据源轮询 ----------
  // next profile 单会话作用域：status.scopedSession 非空时新会话名只能用它
  const statusQ = useQuery({
    queryKey: ['gc-herdr-status'],
    queryFn: fetchHerdrStatus,
    staleTime: 30_000,
  })
  const sessionsQ = useQuery({
    queryKey: ['gc-sessions'],
    queryFn: fetchHerdrSessions,
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: false,
  })
  const snapshotQ = useQuery({
    queryKey: ['gc-snapshot'],
    queryFn: fetchHerdrSnapshot,
    staleTime: 2_000,
    refetchInterval: (query) => {
      const panes = query.state.data?.panes ?? []
      const busy = panes.some(
        (pane) => pane.agent && (pane.agent_status === 'working' || pane.agent_status === 'blocked'),
      )
      return busy ? 2_000 : POLL_MS
    },
    refetchOnWindowFocus: false,
  })
  const ledgerQ = useQuery({
    queryKey: ['gc-chat-ledger'],
    queryFn: fetchChatLedger,
    staleTime: 15_000,
  })

  const ledgerWorkspaces = ledgerQ.data?.workspaces ?? []
  const ledgerThreads = ledgerQ.data?.threads ?? []
  const workspacePaths = useMemo(
    () => ledgerWorkspaces.map((ws) => ws.path),
    [ledgerWorkspaces],
  )

  // Cockpit 4.0: 团队区查询
  const teamConfigQ = useQuery({
    queryKey: ['team-config'],
    queryFn: fetchTeamConfig,
    staleTime: 60_000,
  })
  const teamEnabled = !!(teamConfigQ.data?.team_hub && teamConfigQ.data?.human_auth)

  const teamAuthQ = useQuery({
    queryKey: ['team-auth-status'],
    queryFn: teamAuthStatus,
    enabled: teamEnabled,
    staleTime: 30_000,
  })

  const teamBindingsQ = useQuery({
    queryKey: ['team-bindings'],
    queryFn: teamSessionBindings,
    enabled: teamEnabled && teamAuthQ.data?.logged_in === true,
    staleTime: 30_000,
    refetchInterval: teamEnabled && teamAuthQ.data?.logged_in === true
      ? TEAM_BINDINGS_REFRESH_MS
      : false,
  })

  const teamProjectsQ = useQuery({
    queryKey: ['team-projects'],
    queryFn: listTeamProjects,
    enabled: teamEnabled && teamAuthQ.data?.logged_in === true,
    staleTime: 30_000,
    refetchInterval: teamEnabled && teamAuthQ.data?.logged_in === true ? 5_000 : false,
  })

  const teamTopics = useMemo(() => {
    const bySlug = new Map<string, TeamTopic>()
    for (const topic of teamProjectsQ.data ?? []) bySlug.set(topic.slug, topic)
    for (const topic of teamBindingsQ.data?.topics ?? []) {
      if (!bySlug.has(topic.slug)) bySlug.set(topic.slug, topic)
    }
    return [...bySlug.values()]
  }, [teamProjectsQ.data, teamBindingsQ.data])

  const [teamActiveTopic, setTeamActiveTopic] = useState<string | null>(null)
  const teamMembersQ = useQuery({
    queryKey: ['team-members', teamActiveTopic],
    queryFn: () => listTeamMembers(teamActiveTopic!),
    enabled: teamEnabled && teamAuthQ.data?.logged_in === true && !!teamActiveTopic,
    staleTime: 30_000,
    refetchInterval: teamEnabled && teamAuthQ.data?.logged_in === true ? 5_000 : false,
  })

  const handleTeamLogin = useCallback(async (username: string, password: string) => {
    await teamLogin(username, password)
    queryClient.invalidateQueries({ queryKey: ['team-auth-status'] })
    queryClient.invalidateQueries({ queryKey: ['team-bindings'] })
    queryClient.invalidateQueries({ queryKey: ['team-projects'] })
  }, [queryClient])

  const handleTeamRegister = useCallback(async (input: {
    username: string
    displayName: string
    password: string
    inviteCode: string
  }) => {
    await teamRegister(input)
  }, [])

  const handleTeamLogout = useCallback(async () => {
    await teamLogout()
    queryClient.invalidateQueries({ queryKey: ['team-auth-status'] })
    queryClient.invalidateQueries({ queryKey: ['team-bindings'] })
    queryClient.invalidateQueries({ queryKey: ['team-projects'] })
    setTeamActiveTopic(null)
  }, [queryClient])

  const handleTeamChangePassword = useCallback(async (newPassword: string) => {
    await teamChangePassword(newPassword)
  }, [])

  const handleTeamJoin = useCallback(async (projectSlug: string, mentionHandle: string) => {
    await requestTeamJoin(projectSlug, mentionHandle)
    await queryClient.invalidateQueries({ queryKey: ['team-projects'] })
  }, [queryClient])

  const handleTeamBindSession = useCallback(async (projectSlug: string, sessionName: string) => {
    try {
      await teamBindSession(projectSlug, sessionName, false)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'conflict') {
        if (window.confirm('该 Session 或话题已有绑定。改绑会踢掉前一台，继续？')) {
          await teamBindSession(projectSlug, sessionName, true)
        } else {
          return
        }
      } else {
        throw err
      }
    }
    queryClient.invalidateQueries({ queryKey: ['team-bindings'] })
  }, [queryClient])

  const handleTeamSelectTopic = useCallback((projectSlug: string) => {
    setTeamActiveTopic(projectSlug)
    if (location.pathname !== routePatterns.chat) {
      navigate(routes.chat(), { replace: true })
    }
  }, [location.pathname, navigate])

  const liveRows = useMemo(
    () => buildSessionRows(sessionsQ.data ?? [], snapshotQ.data ?? null),
    [sessionsQ.data, snapshotQ.data],
  )

  // 工作区 = 账本登记；会话按 thread.herdr_session 挂到工作区下
  const { groups: workspaceGroups, ungrouped } = useMemo(
    () => groupByLedger(liveRows, ledgerWorkspaces, ledgerThreads),
    [liveRows, ledgerWorkspaces, ledgerThreads],
  )
  const rows = useMemo(
    () => [...workspaceGroups.flatMap((g) => g.rows), ...ungrouped],
    [workspaceGroups, ungrouped],
  )

  // ---------- 当前会话 ----------
  const [activeSession, setActiveSession] = useState<string | null>(
    () => urlSession ?? loadActiveSession(),
  )
  const didInitRef = useRef(false)
  useEffect(() => {
    if (didInitRef.current || sessionsQ.isPending) return
    didInitRef.current = true
    const names = new Set(rows.map((r) => r.name))
    if (activeSession && names.has(activeSession)) return
    const first = rows[0]?.name ?? null
    if (first && first !== activeSession) setActiveSession(first)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionsQ.isPending, rows])

  // 当前会话消失（被停止/删除）→ 切到第一个可用会话，并改掉 URL，避免和 ?session= 互踢
  useEffect(() => {
    if (isSettings || isTeamAdmin || !didInitRef.current || sessionsQ.isPending) return
    if (sessionsQ.isFetching && rows.length === 0) return
    if (activeSession && !rows.some((r) => r.name === activeSession)) {
      const next = rows[0]?.name ?? null
      if (next) {
        setActiveSession(next)
        saveActiveSession(next)
        setSearchParams({ session: next }, { replace: true })
      } else {
        setActiveSession(null)
        clearActiveSession()
        setSearchParams({}, { replace: true })
      }
    }
  }, [isSettings, activeSession, rows, sessionsQ.isPending, sessionsQ.isFetching, setSearchParams])

  // ---------- 瀑布流状态机（ref 必须在 selectSession 之前） ----------
  const [entries, setEntries] = useState<ChatEntry[]>(() =>
    activeSession ? restoreLocalEntries(activeSession) : [],
  )
  const [recalledIds, setRecalledIds] = useState<Set<string>>(() => new Set())
  const [onlyPane, setOnlyPane] = useState<string | null>(null) // 「只看 TA」过滤
  const [previewFile, setPreviewFile] = useState<string | null>(null) // 主区文件预览（占瀑布流位）
  const [composer, setComposer] = useState(() =>
    activeSession ? loadComposerDraft(activeSession) : '',
  )
  const entrySeq = useRef(0)
  const knownMembersRef = useRef<Map<string, string>>(new Map())
  const skipSaveRef = useRef(true)
  const entriesSessionRef = useRef<string | null>(activeSession)
  const memberSessionRef = useRef<string | null>(null)

  const resetSessionLocal = useCallback((name: string | null) => {
    entriesSessionRef.current = name
    skipSaveRef.current = true
    setEntries(name ? restoreLocalEntries(name) : [])
    setRecalledIds(new Set())
    knownMembersRef.current = new Map()
    memberSessionRef.current = null
    setOnlyPane(null)
    setPreviewFile(null)
    setComposer(name ? loadComposerDraft(name) : '')
  }, [])

  const urlSessionRef = useRef(urlSession)
  const selectSession = useCallback(
    (name: string) => {
      setTeamActiveTopic(null)
      resetSessionLocal(name)
      setActiveSession(name)
      saveActiveSession(name)
      if (
        location.pathname === routePatterns.settings
        || location.pathname === routePatterns.team
      ) {
        navigate(routes.chat({ session: name }), { replace: true })
      } else {
        setSearchParams({ session: name }, { replace: true })
      }
      queryClient.removeQueries({ queryKey: ['gc-mail'], type: 'inactive' })
      if (shouldRefreshMembersOnSelect(activeSession, name)) {
        void queryClient.invalidateQueries({ queryKey: ['gc-snapshot'] })
      }
    },
    [activeSession, resetSessionLocal, location.pathname, navigate, setSearchParams, queryClient],
  )

  useEffect(() => {
    if (isSettings || isTeamAdmin) return
    const urlChanged = urlSessionRef.current !== urlSession
    urlSessionRef.current = urlSession
    const names = rows.map((row) => row.name)
    if (!shouldFollowUrlSession(
      urlSession, activeSession, names, didInitRef.current, urlChanged,
    )) {
      if (
        didInitRef.current
        && urlChanged
        && urlSession
        && urlSession !== activeSession
        && !names.includes(urlSession)
      ) {
        if (activeSession) setSearchParams({ session: activeSession }, { replace: true })
        else setSearchParams({}, { replace: true })
      }
      return
    }
    if (!urlSession) return
    resetSessionLocal(urlSession)
    setActiveSession(urlSession)
  }, [isSettings, urlSession, activeSession, rows, resetSessionLocal, setSearchParams])

  // 当前会话的项目根目录（新成员 workdir / 目录树默认根）
  const activeRoot = useMemo(
    () => rows.find((r) => r.name === activeSession)?.root ?? null,
    [rows, activeSession],
  )

  // ---------- 成员 + leader ----------
  const members = useMemo(() => {
    if (!activeSession) return []
    return withLeader(
      activeSession,
      membersOfSession(snapshotQ.data ?? null, activeSession),
      snapshotQ.data?.session_leaders?.[activeSession],
    )
  }, [activeSession, snapshotQ.data])
  const leader = members.find((m) => m.isLeader) ?? null
  const busyMembers = members.filter(isBusyMember)
  const [nowTick, setNowTick] = useState(() => Date.now())
  useEffect(() => {
    if (busyMembers.length === 0) return
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [busyMembers.length])

  const mailPrimedRef = useRef<string | null>(null)
  const [mailStreamLive, setMailStreamLive] = useState(false)
  useEffect(() => {
    if (!activeSession) {
      setMailStreamLive(false)
      return
    }
    const session = activeSession
    if (typeof EventSource !== 'function') {
      setMailStreamLive(false)
      return
    }
    let source: EventSource
    try {
      source = new EventSource(sessionMailStreamUrl(session), { withCredentials: true })
    } catch {
      setMailStreamLive(false)
      return
    }
    const apply = (event: string) => (ev: MessageEvent<string>) => {
      setMailStreamLive(true)
      queryClient.setQueryData<SessionMailMessage[]>(['gc-mail', session], (current) =>
        applyMailStreamEvent(current, event, ev.data, session),
      )
    }
    source.addEventListener('snapshot', apply('snapshot'))
    source.addEventListener('message', apply('message'))
    source.addEventListener('replace', apply('replace'))
    source.addEventListener('receipt', apply('receipt'))
    source.onerror = () => setMailStreamLive(false)
    return () => {
      source.close()
      setMailStreamLive(false)
    }
  }, [activeSession, queryClient])
  const mailQ = useQuery({
    queryKey: ['gc-mail', activeSession],
    queryFn: async ({ queryKey }) => {
      const name = queryKey[1]
      if (typeof name !== 'string' || !name) return []
      const cached = queryClient.getQueryData<SessionMailMessage[]>(['gc-mail', name])
      if (mailPrimedRef.current === name && cached && cached.length > 0) {
        const next = await fetchSessionMail(name, 'all')
        return preferLedgerMail(cached, next)
      }
      const first = await fetchSessionMail(name, 'ledger')
      mailPrimedRef.current = name
      void fetchSessionMail(name, 'all')
        .then((full) => {
          queryClient.setQueryData<SessionMailMessage[]>(['gc-mail', name], (current) =>
            preferLedgerMail(current, full),
          )
        })
        .catch(() => {
          /* 账本已出，Hub 补漏失败不挡首屏 */
        })
      return first
    },
    enabled: !!activeSession,
    staleTime: 4_000,
    refetchInterval: mailStreamLive ? POLL_MS : busyMembers.length > 0 ? 2_000 : POLL_MS,
    refetchOnWindowFocus: false,
  })
  // 文件树挂工作区目录；未分组/无目录不展示
  const fileRoot = activeRoot
  const membersRef = useRef<ChatMember[]>(members)
  membersRef.current = members

  const pushEntries = useCallback((added: ChatEntry[]) => {
    if (added.length === 0) return
    setEntries((prev) => {
      const next = [...prev, ...added]
      return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next
    })
  }, [])

  // 切换会话：只读回本会话本地气泡，不把上一群的进出/气泡带过来
  useEffect(() => {
    if (entriesSessionRef.current === activeSession) return
    resetSessionLocal(activeSession)
  }, [activeSession, resetSessionLocal])

  useEffect(() => {
    if (!activeSession) return
    if (skipSaveRef.current) {
      skipSaveRef.current = false
      return
    }
    if (entriesSessionRef.current !== activeSession) return
    saveLocalEntries(
      activeSession,
      entries.filter((entry) => entry.kind !== 'event' || !isMemberRosterEvent(entry.text)),
    )
  }, [activeSession, entries])

  useEffect(() => {
    if (!activeSession) return
    saveComposerDraft(activeSession, composer)
  }, [activeSession, composer])

  const memberKey = members.map((m) => m.paneId).join(',')
  useEffect(() => {
    if (!activeSession) return
    if (shouldSeedMemberRoster(
      memberSessionRef.current, activeSession, knownMembersRef.current.size,
    )) {
      memberSessionRef.current = activeSession
      knownMembersRef.current = new Map(members.map((m) => [m.paneId, m.name]))
      return
    }
    // 只记成员进出。TUI 摘要每几秒都在变（转圈、时钟），推进瀑布流会像整页刷新。
    if (snapshotQ.isFetching && members.length === 0 && knownMembersRef.current.size > 0) {
      return
    }
    const cur = new Map(members.map((m) => [m.paneId, m.name]))
    const added: ChatEntry[] = []
    const now = Date.now()
    for (const [id, name] of cur) {
      if (!knownMembersRef.current.has(id)) {
        added.push({ id: `e${++entrySeq.current}`, kind: 'event', text: `${name} 加入了群聊`, ts: now })
      }
    }
    for (const [id, name] of knownMembersRef.current) {
      if (!cur.has(id)) {
        added.push({ id: `e${++entrySeq.current}`, kind: 'event', text: `${name} 离开了群聊`, ts: now })
      }
    }
    knownMembersRef.current = cur
    pushEntries(added)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSession, memberKey, pushEntries])

  // ---------- 发送（@谁发给谁；不@默认 leader） ----------
  const [sending, setSending] = useState(false)
  const [attaching, setAttaching] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  const onAttach = useCallback(async (file: File) => {
    if (!activeSession || attaching) return
    const sessionAtAttach = activeSession
    try {
      await requireAuthenticated()
    } catch (e) {
      pushEntries([
        {
          id: `e${++entrySeq.current}`,
          kind: 'error',
          text: `附件上传失败：${e instanceof ApiError ? e.message : String(e)}。请先登录再上传。`,
          ts: Date.now(),
        },
      ])
      return
    }
    setAttaching(true)
    try {
      const saved = await uploadChatFile(activeSession, file)
      if (entriesSessionRef.current === sessionAtAttach) {
        setComposer((prev) => appendAttachMarkup(prev, saved.filename, saved.path))
        requestAnimationFrame(() => inputRef.current?.focus())
      }
    } catch (e) {
      if (entriesSessionRef.current === sessionAtAttach) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `附件上传失败：${e instanceof ApiError ? e.message : String(e)}`,
            ts: Date.now(),
          },
        ])
      }
    } finally {
      setAttaching(false)
    }
  }, [activeSession, attaching, pushEntries])

  const onSend = useCallback(async (delivery: ChatDelivery = 'queue') => {
    const text = composer.trim()
    if (!text || !activeSession || sending) return
    const sessionAtSend = activeSession
    const targets = parseMentionTargets(text, membersRef.current)
    const dest = targets.length > 0 ? targets : leader ? [leader] : []
    if (dest.length === 0) {
      pushEntries([
        { id: `e${++entrySeq.current}`, kind: 'error', text: '会话里还没有成员，请先在右侧添加。', ts: Date.now() },
      ])
      return
    }
    const broadcast = hasBroadcastMention(text)
    // @all 广播：账本存 all 标记，后端投递时展开全员；@一人/@多人即定向：只投递被 @ 者、不进 Hub、小圈可见。
    const mailTo = broadcast
      ? ['all']
      : dest.map((m) => m.mailName || m.name).filter(Boolean)
    if (mailTo.length === 0) {
      pushEntries([
        { id: `e${++entrySeq.current}`, kind: 'error', text: '成员还没有 Agent Mail 花名，无法发信。', ts: Date.now() },
      ])
      return
    }
    setSending(true)
    const direct = !broadcast
    try {
      const sent = await sendSessionMail(activeSession, text, mailTo, { delivery, direct, source: 'composer' })
      saveComposerDraft(sessionAtSend, '')
      if (entriesSessionRef.current === sessionAtSend) {
        setComposer('')
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'me',
            text,
            to: broadcast ? ['all'] : dest.map((m) => m.name),
            mailTo,
            ts: Date.now(),
            delivery,
            direct,
            source: 'composer',
            receipt: 'queued',
          },
        ])
      }
      void queryClient.invalidateQueries({ queryKey: ['gc-mail', activeSession] })
      if (sent.mail_error && entriesSessionRef.current === sessionAtSend) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `已记下这条，但 Agent Mail 没发出去：${sent.mail_error}`,
            ts: Date.now(),
          },
        ])
      }
    } catch (e) {
      if (entriesSessionRef.current === sessionAtSend) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `发送失败：${e instanceof ApiError ? e.message : String(e)}。字还在输入框，登录后不用重打。`,
            ts: Date.now(),
          },
        ])
      }
    } finally {
      setSending(false)
    }
  }, [composer, activeSession, sending, leader, pushEntries, queryClient])

  const onRecall = useCallback(
    async (entry: Extract<ChatEntry, { kind: 'me' }>) => {
      if (entry.recalled || !activeSession || !canRecallEntry(entry.ts)) return
      try {
        await requireAuthenticated()
      } catch (e) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `撤回失败：${e instanceof ApiError ? e.message : String(e)}。请先登录再撤回。`,
            ts: Date.now(),
          },
        ])
        return
      }
      setRecalledIds((prev) => {
        const next = new Set(prev)
        next.add(entry.id)
        next.add(`text:${entry.text}`)
        return next
      })
      setEntries((prev) =>
        prev.map((item) =>
          item.id === entry.id && item.kind === 'me' ? { ...item, recalled: true } : item,
        ),
      )
      try {
        await sendSessionMail(activeSession, recallNotice(entry.text), entry.mailTo, {
          direct: entry.direct,
          source: 'composer',
        })
        void queryClient.invalidateQueries({ queryKey: ['gc-mail', activeSession] })
      } catch (e) {
        setRecalledIds((prev) => {
          const next = new Set(prev)
          next.delete(entry.id)
          next.delete(`text:${entry.text}`)
          return next
        })
        setEntries((prev) =>
          prev.map((item) =>
            item.id === entry.id && item.kind === 'me' ? { ...item, recalled: false } : item,
          ),
        )
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `撤回失败：${e instanceof ApiError ? e.message : String(e)}`,
            ts: Date.now(),
          },
        ])
      }
    },
    [activeSession, pushEntries, queryClient],
  )

  const onEdit = useCallback((entry: Extract<ChatEntry, { kind: 'me' }>) => {
    if (entry.recalled) return
    setComposer(entry.text)
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [])

  const [interactMember, setInteractMember] = useState<ChatMember | null>(null)
  const [terminalSession, setTerminalSession] = useState<string | null>(null)

  const insertMention = useCallback((m: ChatMember) => {
    setComposer((prev) => {
      const needsSpace = prev.length > 0 && !/\s$/.test(prev)
      return `${prev}${needsSpace ? ' ' : ''}@${m.name} `
    })
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [])

  const mailEntries = useMemo(() => {
    if (!activeSession) return []
    const mapped = mailToEntries(mailQ.data ?? [], members)
    return mapped.map((entry) =>
      entry.kind === 'me' && (recalledIds.has(entry.id) || recalledIds.has(`text:${entry.text}`))
        ? { ...entry, recalled: true }
        : entry,
    )
  }, [activeSession, mailQ.isPending, mailQ.data, members, recalledIds])

  const visibleEntries = useMemo(() => {
    const local = entriesSessionRef.current === activeSession ? entries : []
    const extras = local.filter((entry) => {
      if (entry.kind === 'me') return !mailCoversLocalMe(mailEntries, entry)
      return entry.kind === 'event' || entry.kind === 'error'
    })
    const historical = [...mailEntries, ...extras].sort((a, b) => {
      if (a.ts !== b.ts) return a.ts - b.ts
      return a.id.localeCompare(b.id)
    })
    const live = typingEntries(members, nowTick)
    const keepAgent = (entry: ChatEntry) => entry.kind !== 'agent' || entry.paneId === onlyPane
    const keepDirect = (entry: ChatEntry) => {
      if (entry.kind !== 'agent' && entry.kind !== 'me') return true
      return isDirectMessageVisible(entry, onlyPane, members)
    }
    const filtered = onlyPane
      ? [...historical.filter(keepAgent).filter(keepDirect), ...live.filter(keepAgent)]
      : [...historical.filter(keepDirect), ...live]
    return filtered
  }, [activeSession, entries, mailEntries, members, nowTick, onlyPane])
  const onlyMember = onlyPane ? members.find((m) => m.paneId === onlyPane) ?? null : null

  // ---------- 工作区与新会话 ----------
  const [addWorkspaceOpen, setAddWorkspaceOpen] = useState(false)
  const [wizardTarget, setWizardTarget] = useState<{ id: string; root: string } | null>(null)
  const [sessionAction, setSessionAction] = useState<
    { kind: 'stop' | 'delete'; name: string } | null
  >(null)
  const [sessionActionBusy, setSessionActionBusy] = useState(false)
  const [bindPrompt, setBindPrompt] = useState<{
    id: string
    label: string
    candidates: ChatBindCandidate[]
  } | null>(null)
  const [bindBusy, setBindBusy] = useState(false)
  const addMemberKey = 0
  const [detailsTab, setDetailsTab] = useState<DetailsTab>('members')
  useEffect(() => {
    if (!fileRoot && detailsTab === 'files') setDetailsTab('members')
  }, [fileRoot, detailsTab])

  // 侧栏「新会话」：无工作区先添加工作区，否则在当前（或第一个）工作区创建
  const startSession = useCallback(() => {
    if (workspaceGroups.length === 0) {
      setAddWorkspaceOpen(true)
      return
    }
    const preferred = workspaceGroups.find((g) => g.root === activeRoot) ?? workspaceGroups[0]
    setWizardTarget({ id: preferred.id, root: preferred.root })
  }, [workspaceGroups, activeRoot])

  const onCreated = useCallback(
    (session: string) => {
      setWizardTarget(null)
      selectSession(session)
      queryClient.invalidateQueries({ queryKey: ['gc-sessions'] })
      queryClient.invalidateQueries({ queryKey: ['gc-snapshot'] })
      queryClient.invalidateQueries({ queryKey: ['gc-chat-ledger'] })
    },
    [selectSession, queryClient],
  )

  const refreshLedger = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['gc-chat-ledger'] }),
    [queryClient],
  )

  const refreshSessions = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['gc-sessions'] })
    queryClient.invalidateQueries({ queryKey: ['gc-snapshot'] })
    queryClient.invalidateQueries({ queryKey: ['gc-chat-ledger'] })
  }, [queryClient])

  const runSessionAction = useCallback(async () => {
    if (!sessionAction || sessionActionBusy) return
    const { kind, name } = sessionAction
    try {
      await requireAuthenticated()
    } catch (e) {
      pushEntries([
        {
          id: `e${++entrySeq.current}`,
          kind: 'error',
          text: `${kind === 'stop' ? '停止' : '删除'}会话失败：${e instanceof ApiError ? e.message : String(e)}。请先登录再提交。`,
          ts: Date.now(),
        },
      ])
      return
    }
    setSessionActionBusy(true)
    try {
      if (kind === 'stop') {
        await stopHerdrSession(name)
      } else {
        const row = rows.find((item) => item.name === name)
        if (!row || row.status !== 'stopped') {
          try {
            await stopHerdrSession(name)
          } catch (cause) {
            if (!isAlreadyStoppedError(cause)) throw cause
          }
        }
        await deleteHerdrSession(name)
      }
      if (kind === 'delete') {
        const next = nextSessionAfterRemoval(rows.map((row) => row.name), name, activeSession)
        if (next && next !== activeSession) {
          selectSession(next)
        } else if (!next) {
          resetSessionLocal(null)
          setActiveSession(null)
          clearActiveSession()
          if (location.pathname !== routePatterns.settings) {
            setSearchParams({}, { replace: true })
          }
        }
      }
      setSessionAction(null)
      refreshSessions()
    } catch (e) {
      pushEntries([
        {
          id: `e${++entrySeq.current}`,
          kind: 'error',
          text: `${kind === 'stop' ? '停止' : '删除'}会话失败：${e instanceof ApiError ? e.message : String(e)}`,
          ts: Date.now(),
        },
      ])
    } finally {
      setSessionActionBusy(false)
    }
  }, [sessionAction, sessionActionBusy, rows, activeSession, refreshSessions, pushEntries, selectSession, resetSessionLocal, setSearchParams, location.pathname])

  const onOpenWorkspace = useCallback(
    async (id: string) => {
      const group = workspaceGroups.find((g) => g.id === id)
      try {
        await requireAuthenticated()
        const result = await openChatWorkspace(id)
        if (result.thread) {
          selectSession(result.thread.herdr_session)
          refreshSessions()
          if ((result.bound?.length ?? 0) > 0) {
            pushEntries([{
              id: `e${++entrySeq.current}`,
              kind: 'event',
              text: `已把同目录会话绑进此工作区：${result.bound!.map((row) => row.herdr_session).join('、')}`,
              ts: Date.now(),
            }])
          }
          if (result.agent_mail && result.agent_mail.ok === false) {
            pushEntries([
              {
                id: `e${++entrySeq.current}`,
                kind: 'error',
                text: `会话已打开，但成员 Agent Mail 未登记：${result.agent_mail.reason || result.agent_mail.error || '未知原因'}`,
                ts: Date.now(),
              },
            ])
          }
          return
        }
        if (result.needs_bind) {
          setBindPrompt({
            id,
            label: group?.label || group?.root || '工作区',
            candidates: result.candidates ?? [],
          })
          return
        }
        if (result.empty && group) {
          setWizardTarget({ id: group.id, root: group.root })
        }
      } catch (e) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `打开工作区失败：${e instanceof ApiError ? e.message : String(e)}`,
            ts: Date.now(),
          },
        ])
      }
    },
    [workspaceGroups, selectSession, refreshSessions, pushEntries],
  )

  const onBindCandidate = useCallback(
    async (session: string) => {
      if (!bindPrompt || bindBusy) return
      try {
        await requireAuthenticated()
      } catch (e) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `绑定会话失败：${e instanceof ApiError ? e.message : String(e)}。请先登录再绑定。`,
            ts: Date.now(),
          },
        ])
        return
      }
      setBindBusy(true)
      try {
        const thread = await bindChatWorkspace(bindPrompt.id, session)
        setBindPrompt(null)
        selectSession(thread.herdr_session)
        refreshSessions()
      } catch (e) {
        pushEntries([
          {
            id: `e${++entrySeq.current}`,
            kind: 'error',
            text: `绑定会话失败：${e instanceof ApiError ? e.message : String(e)}`,
            ts: Date.now(),
          },
        ])
      } finally {
        setBindBusy(false)
      }
    },
    [bindPrompt, bindBusy, selectSession, refreshSessions, pushEntries],
  )

  const onRemoveWorkspace = useCallback(
    (id: string) => {
      const group = workspaceGroups.find((g) => g.id === id)
      const label = group?.label || group?.root || id
      if (!window.confirm(`移除工作区「${label}」？\n只从侧栏拿掉，不会删磁盘目录，也不会停止 herdr。`)) {
        return
      }
      deleteChatWorkspace(id)
        .then(refreshLedger)
        .catch((e) => {
          pushEntries([
            {
              id: `e${++entrySeq.current}`,
              kind: 'error',
              text: `移除工作区失败：${e instanceof ApiError ? e.message : String(e)}`,
              ts: Date.now(),
            },
          ])
        })
    },
    [workspaceGroups, refreshLedger, pushEntries],
  )

  return (
    <div className="gc-shell">
      <AppFrame
        detailsAvailable={!isSettings && !isTeamAdmin && (!!activeSession || !!teamActiveTopic)}
        sidebar={
          <SidebarRoot
            onStartSession={startSession}
            onOpenSettings={() => { navigate(routes.settings()) }}
          >
            {(wide) => (
              <NarrowAwareBrowser
                groups={workspaceGroups}
                ungrouped={ungrouped}
                activeSession={activeSession}
                loading={sessionsQ.isPending || ledgerQ.isPending}
                wide={wide}
                onSelect={selectSession}
                onAddWorkspace={() => { setAddWorkspaceOpen(true) }}
                onNewSession={(root) => {
                  const hit = workspaceGroups.find((g) => g.root === root)
                  if (hit) setWizardTarget({ id: hit.id, root: hit.root })
                }}
                onRemoveWorkspace={onRemoveWorkspace}
                onStopSession={(name) => { setSessionAction({ kind: 'stop', name }) }}
                onDeleteSession={(name) => { setSessionAction({ kind: 'delete', name }) }}
                onOpenWorkspace={(id) => { void onOpenWorkspace(id) }}
                teamEnabled={teamEnabled}
                teamLoggedIn={teamAuthQ.data?.logged_in ?? false}
                teamUsername={teamAuthQ.data?.username ?? null}
                teamIsAdmin={(teamAuthQ.data?.roles ?? []).includes('admin')}
                teamTopics={teamTopics}
                teamBindings={teamBindingsQ.data?.bindings ?? []}
                teamSessions={teamBindingsQ.data?.sessions ?? []}
                teamActiveTopic={teamActiveTopic}
                onTeamLogin={handleTeamLogin}
                onTeamRegister={handleTeamRegister}
                onTeamLogout={handleTeamLogout}
                onTeamChangePassword={handleTeamChangePassword}
                onTeamJoin={handleTeamJoin}
                onTeamBindSession={handleTeamBindSession}
                onTeamSelectTopic={handleTeamSelectTopic}
                onOpenTeamAdmin={() => navigate(routes.team())}
              />
            )}
          </SidebarRoot>
        }
        details={
          <DetailsPanel
            tab={detailsTab}
            onTabChange={setDetailsTab}
            members={members}
            membersLoading={snapshotQ.isPending || snapshotQ.isFetching}
            session={activeSession}
            workdir={activeRoot ?? leader?.cwd ?? null}
            onMention={insertMention}
            onFilter={(m) => { setOnlyPane(m.paneId) }}
            onInteract={setInteractMember}
            onOpenTerminal={() => { if (activeSession) setTerminalSession(activeSession) }}
            onMembersChanged={() => { queryClient.invalidateQueries({ queryKey: ['gc-snapshot'] }) }}
            externalAddSignal={addMemberKey}
            teamTopic={teamActiveTopic}
            teamMembers={teamMembersQ.data ?? []}
            teamMembersLoading={teamMembersQ.isPending && !!teamActiveTopic}
            teamMembersError={
              teamActiveTopic && teamMembersQ.isError
                ? teamMembersQ.error instanceof Error
                  ? teamMembersQ.error.message
                  : '读取团队成员失败'
                : null
            }
            fileRoot={fileRoot}
            onPreview={setPreviewFile}
          />
        }
      >
        <section className="gc-main">
          <div className="gc-toolbar">
            <span className="gc-toolbar-title">{isSettings ? '设置' : isTeamAdmin ? '团队管理' : (teamActiveTopic ? (teamTopics.find((t) => t.slug === teamActiveTopic)?.name ?? teamActiveTopic) : (activeSession ?? '群聊'))}</span>
            {!isSettings && activeRoot && <span className="gc-toolbar-sub">{rootBase(activeRoot)}</span>}
            {!isSettings && onlyMember && (
              <button
                type="button"
                className="gc-only-chip"
                onClick={() => setOnlyPane(null)}
                title="取消过滤"
              >
                只看 {onlyMember.name} ✕
              </button>
            )}
            {!isSettings && (
              <div className="gc-toolbar-actions">
                <DetailsTabButton tab="members" current={detailsTab} onSelect={setDetailsTab}>
                  成员
                </DetailsTabButton>
                {fileRoot && (
                  <DetailsTabButton tab="files" current={detailsTab} onSelect={setDetailsTab}>
                    文件
                  </DetailsTabButton>
                )}
              </div>
            )}
          </div>

          {isSettings ? (
            <div className="gc-settings">
              <SettingsPage />
            </div>
          ) : isTeamAdmin ? (
            <div className="gc-settings">
              <TeamAdminPage />
            </div>
          ) : teamActiveTopic ? (
            <TeamTimeline
              topic={teamActiveTopic}
              topicName={
                teamTopics.find((t) => t.slug === teamActiveTopic)?.name
                ?? teamActiveTopic
              }
            />
          ) : previewFile && activeSession ? (
            <FilePreview
              session={activeSession}
              path={previewFile}
              onClose={() => setPreviewFile(null)}
            />
          ) : (
            <>
              {!isSettings && activeSession && (
                <AgentMailStatusBar session={activeSession} members={members} />
              )}
              {mailQ.isError && (
                <div className="gc-event">
                  邮件历史读失败：
                  {mailQ.error instanceof ApiError ? mailQ.error.message : String(mailQ.error)}
                </div>
              )}
              <Waterfall
                key={activeSession ?? 'none'}
                entries={visibleEntries}
                hasSession={!!activeSession}
                ungrouped={!activeSession || !ledgerThreads.some((th) => th.herdr_session === activeSession)}
                onRecall={(entry) => { void onRecall(entry) }}
                onEdit={onEdit}
                onOpenAgent={(entry) => {
                  const member = members.find((item) => item.paneId === entry.paneId)
                  if (member) setInteractMember(member)
                }}
                onOpenPath={(path) => {
                  setPreviewFile(path)
                }}
              />
            </>
          )}

          {!isSettings && !teamActiveTopic && busyMembers.length > 0 && (
            <div className="gc-busy-icons" role="status" aria-label="工作中或等你输入">
              {busyMembers.map((m) => {
                const unread = m.unread && m.unread > 0
                  ? (m.unread > 99 ? '99+' : String(m.unread))
                  : ''
                const label = m.status === 'blocked'
                  ? `${m.name} 等你输入`
                  : `${m.name} 正在回复`
                const full = unread ? `${label}，${m.unread} 条未读` : label
                return (
                  <button
                    key={m.paneId}
                    type="button"
                    className={`gc-busy-icon${m.status === 'blocked' ? ' is-blocked' : ' is-working'}`}
                    title={full}
                    aria-label={full}
                    onClick={() => setInteractMember(m)}
                  >
                    <span
                      className={`gc-member-avatar gc-member-avatar--${m.kind.toLowerCase()}`}
                      style={{ background: avatarColor(m.kind) }}
                      aria-hidden
                    >
                      <AgentIcon kind={m.kind} size={16} />
                    </span>
                    {unread && <span className="gc-unread-badge gc-unread-badge--icon">{unread}</span>}
                  </button>
                )
              })}
            </div>
          )}
          {!isSettings && !teamActiveTopic && (
            <Composer
              session={activeSession || ''}
              members={members}
              leader={leader}
              value={composer}
              onChange={setComposer}
              onSend={onSend}
              onAttach={(file) => { void onAttach(file) }}
              attaching={attaching}
              disabled={!activeSession || sending}
              inputRef={inputRef}
            />
          )}
        </section>
      </AppFrame>

      {addWorkspaceOpen && (
        <AddWorkspaceModal
          roots={workspacePaths}
          onClose={() => setAddWorkspaceOpen(false)}
          onAdded={refreshLedger}
        />
      )}

      <NewSessionWizard
        open={wizardTarget !== null}
        workspaceId={wizardTarget?.id ?? null}
        workdir={wizardTarget?.root ?? null}
        fixedSessionName={statusQ.data?.scopedSession ?? null}
        existingSessions={rows.map((r) => r.name)}
        onClose={() => setWizardTarget(null)}
        onCreated={onCreated}
      />

      {bindPrompt && (
        <div className="gc-modal-bg" onClick={bindBusy ? undefined : () => { setBindPrompt(null) }}>
          <div className="gc-modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="gc-modal-title">绑定已有会话</h3>
            <p className="gc-modal-sub">
              「{bindPrompt.label}」这个目录上已有 herdr，还没记进账本。选一条绑定，不会新建。
            </p>
            <div className="gc-picker-list" role="listbox" aria-label="可绑定会话">
              {bindPrompt.candidates.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className="gc-project-chip"
                  disabled={bindBusy}
                  onClick={() => { void onBindCandidate(c.name) }}
                >
                  <span className="gc-project-name">{c.name}</span>
                  <span className="gc-project-path">{c.status}</span>
                </button>
              ))}
            </div>
            <div className="gc-modal-actions">
              <button
                type="button"
                className="gc-pill-btn"
                disabled={bindBusy}
                onClick={() => { setBindPrompt(null) }}
              >
                取消
              </button>
              <button
                type="button"
                className="gc-pill-btn gc-pill-btn--accent"
                disabled={bindBusy}
                onClick={() => {
                  const group = workspaceGroups.find((g) => g.id === bindPrompt.id)
                  setBindPrompt(null)
                  if (group) setWizardTarget({ id: group.id, root: group.root })
                }}
              >
                还是新建
              </button>
            </div>
          </div>
        </div>
      )}

      {interactMember && activeSession && (
        <AgentInteractModal
          member={interactMember}
          session={activeSession}
          onClose={() => setInteractMember(null)}
        />
      )}

      {terminalSession && (
        <HerdrTerminalModal
          session={terminalSession}
          onClose={() => setTerminalSession(null)}
        />
      )}

      {sessionAction && (
        <div className="gc-modal-bg" onClick={sessionActionBusy ? undefined : () => { setSessionAction(null) }}>
          <div className="gc-modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="gc-modal-title">
              {sessionAction.kind === 'stop' ? '停止会话' : '删除会话'}
            </h3>
            <p className="gc-modal-sub">
              {sessionAction.kind === 'stop'
                ? `停止 herdr session「${sessionAction.name}」？里面的 Agent 会停掉，会话名还在，以后可以再启动。`
                : `删除 herdr session「${sessionAction.name}」？会先停止（如果还在跑），再删掉 herdr 现场和账本里的这条群聊。工作区目录不会动。`}
            </p>
            <div className="gc-modal-actions">
              <button
                type="button"
                className="gc-pill-btn"
                disabled={sessionActionBusy}
                onClick={() => { setSessionAction(null) }}
              >
                取消
              </button>
              <button
                type="button"
                className="gc-pill-btn gc-pill-btn--accent"
                disabled={sessionActionBusy}
                onClick={() => { void runSessionAction() }}
              >
                {sessionActionBusy
                  ? (sessionAction.kind === 'stop' ? '停止中…' : '删除中…')
                  : (sessionAction.kind === 'stop' ? '确认停止' : '确认删除')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
