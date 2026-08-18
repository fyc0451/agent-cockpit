// 群聊工作台纯逻辑：成员模型、leader 持久化、@ 解析、摘要增量 diff、会话归属。
// 全部纯函数 / localStorage 存取，不依赖 React，便于 vitest 单测。

import type { HerdrPane, HerdrSession, HerdrSnapshot } from '../../api/legacyHerdr'

/** 可选的 agent 类型（规格冻结：codex / claude / kimi / opencode / grok） */
export const AGENT_KINDS = ['codex', 'claude', 'kimi', 'opencode', 'grok'] as const

// ---------- 成员模型 ----------

export interface ChatMember {
  paneId: string
  session: string
  kind: string // codex | claude | kimi | ...
  name: string // 群内显示名（花名）
  mailName: string // Agent Mail 收件名；空则不能走邮件
  status: string // idle | working | blocked | done | unknown
  cwd: string
  isLeader: boolean
}

export function leftoverMemberName(name: string, agent: string): boolean {
  const n = name.trim().toLowerCase()
  const a = agent.trim().toLowerCase()
  if (!n) return true
  return n === a || n === `${a}-main` || n.startsWith(`${a}-`)
}

/** @kimi-main / @kimi-agent-* → 该类型唯一在场成员。多人同 kind 不猜。 */
export function mentionMatchesLeftoverAlias(token: string, member: ChatMember, members: ChatMember[]): boolean {
  const kind = member.kind.trim().toLowerCase()
  if (!kind || !leftoverMemberName(token, kind)) return false
  return members.filter((item) => item.kind.toLowerCase() === kind).length === 1
}

/** pane → 群内显示名：花名优先，避开 program-main */
export function memberName(pane: HerdrPane): string {
  const mail = pane.mail_name.trim()
  const display = pane.display_name.trim()
  const agent = pane.agent
  if (mail && !leftoverMemberName(mail, agent)) return mail
  if (display && !leftoverMemberName(display, agent)) return display
  if (mail) return mail
  const tail = pane.pane_id.replace(/^%/, '').slice(-2) || pane.pane_id
  return agent ? `${agent}-${tail}` : pane.pane_id
}

/** 取某会话的全部 agent 成员（过滤非 agent pane），按 pane 顺序（= 加入顺序） */
export function membersOfSession(snapshot: HerdrSnapshot | null, session: string): ChatMember[] {
  if (!snapshot) return []
  const panes = snapshot.panes.filter((p) => p.session === session && p.agent)
  const seen = new Set<string>()
  return panes.map((p) => {
    let name = memberName(p)
    while (seen.has(name)) name = `${name}·`
    seen.add(name)
    return {
      paneId: p.pane_id,
      session: p.session,
      kind: p.agent,
      name,
      mailName: p.mail_name || '',
      status: p.agent_status === 'done' ? 'idle' : p.agent_status || 'unknown',
      cwd: p.cwd,
      isLeader: false,
    }
  })
}

// ---------- leader：第一个加入的 agent；localStorage 持久化，pane 消失后重算 ----------

const LEADER_KEY = (session: string) => `gc:leader:${session}`

export function loadLeaderPaneId(session: string): string | null {
  try {
    return window.localStorage.getItem(LEADER_KEY(session))
  } catch {
    return null
  }
}

export function saveLeaderPaneId(session: string, paneId: string): void {
  try {
    window.localStorage.setItem(LEADER_KEY(session), paneId)
  } catch {
    // 隐私模式等场景下静默降级：leader 退化为每次取第一个成员
  }
}

/** 标记 leader：已存且仍在群中 → 用之；否则取第一个成员并落盘 */
export function withLeader(session: string, members: ChatMember[]): ChatMember[] {
  if (members.length === 0) return members
  const stored = loadLeaderPaneId(session)
  const hit = stored ? members.find((m) => m.paneId === stored) : undefined
  const leaderId = hit ? hit.paneId : members[0].paneId
  if (leaderId !== stored) saveLeaderPaneId(session, leaderId)
  return members.map((m) => ({ ...m, isLeader: m.paneId === leaderId }))
}

// ---------- @ 解析 ----------

/** 光标处是否处于 @ 补全状态：@ 前必须是行首/空白，查询词无换行且 ≤20 字符 */
/** 组字中（含 Linux IME keyCode 229）按回车只确认候选，不发送。 */
export function shouldSendOnEnter(e: {
  key: string
  shiftKey: boolean
  isComposing?: boolean
  keyCode?: number
}): boolean {
  if (e.key !== 'Enter' || e.shiftKey) return false
  if (e.isComposing || e.keyCode === 229) return false
  return true
}

export function mentionQueryAt(value: string, caret: number): { start: number; query: string } | null {
  const before = value.slice(0, caret)
  const atIdx = before.lastIndexOf('@')
  if (atIdx < 0) return null
  const charBefore = atIdx > 0 ? before[atIdx - 1] : ' '
  if (!/[\s\n]/.test(charBefore)) return null
  const query = before.slice(atIdx + 1)
  if (query.includes('\n') || query.length > 20) return null
  return { start: atIdx, query }
}

/** 从文本解析 @目标：@花名 精确（大小写不敏感），@agent类型 命中该类型全部成员；去重 */
export function parseMentionTargets(text: string, members: ChatMember[]): ChatMember[] {
  const tokens = text.match(/@([^\s@]+)/g)
  if (!tokens) return []
  const out: ChatMember[] = []
  const seen = new Set<string>()
  for (const raw of tokens) {
    const token = raw.slice(1).toLowerCase()
    for (const m of members) {
      if (seen.has(m.paneId)) continue
      if (
        m.name.toLowerCase() === token ||
        m.mailName.toLowerCase() === token ||
        m.kind.toLowerCase() === token ||
        (token === 'leader' && m.isLeader) ||
        mentionMatchesLeftoverAlias(token, m, members)
      ) {
        seen.add(m.paneId)
        out.push(m)
      }
    }
  }
  return out
}

// ---------- 摘要增量 diff（行级后缀重叠） ----------

/**
 * prev → next 的增量部分：找 prev 后缀与 next 前缀的最大重叠，只返回新增行。
 * 无任何重叠（会话被清空/轮转）→ 返回 next 全文；无变化 → 空串。
 */
export function diffSummaryLines(prev: string, next: string): string {
  if (!next || next === prev) return ''
  if (!prev) return next
  const prevLines = prev.split('\n')
  const nextLines = next.split('\n')
  const maxOverlap = Math.min(prevLines.length, nextLines.length)
  for (let k = maxOverlap; k > 0; k--) {
    let match = true
    for (let i = 0; i < k; i++) {
      if (prevLines[prevLines.length - k + i] !== nextLines[i]) {
        match = false
        break
      }
    }
    if (match) return nextLines.slice(k).join('\n')
  }
  return next
}

// ---------- 会话归属与项目根目录 ----------

/** root 的展示名（basename） */
/** 瀑布流分段：围栏代码与普通文本拆开，代码默认整段展开。 */
export type MessagePart =
  | { type: 'text'; text: string }
  | { type: 'code'; lang: string; text: string }

export function splitMessageParts(raw: string): MessagePart[] {
  if (!raw) return []
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g
  const parts: MessagePart[] = []
  let last = 0
  let match: RegExpExecArray | null
  while ((match = fence.exec(raw))) {
    if (match.index > last) {
      parts.push({ type: 'text', text: raw.slice(last, match.index) })
    }
    parts.push({
      type: 'code',
      lang: match[1].trim(),
      text: match[2].replace(/\n$/, ''),
    })
    last = match.index + match[0].length
  }
  if (last < raw.length) parts.push({ type: 'text', text: raw.slice(last) })
  return parts.filter((part) => part.type === 'code' || part.text.length > 0)
}

export function rootBase(root: string): string {
  const base = root.replace(/\/+$/, '')
  return base.split('/').pop() || base
}

/** 会话归属的项目根：任取该会话 agent pane 的 cwd，找最长前缀 root */
export function sessionRoot(panes: HerdrPane[], session: string, roots: string[]): string | null {
  let best: string | null = null
  for (const p of panes) {
    if (p.session !== session || !p.agent) continue
    for (const r of roots) {
      const base = r.replace(/\/+$/, '')
      if ((p.cwd === base || p.cwd.startsWith(base + '/')) && (!best || base.length > best.length)) {
        best = base
      }
    }
  }
  return best
}

// ---------- 展示辅助 ----------

const AGENT_EMOJI: Record<string, string> = {
  codex: '🤖',
  claude: '🟣',
  kimi: '🌙',
  opencode: '🧩',
  grok: '⚡',
  qoder: '🛠️',
  qodercli: '🛠️',
  qodercn: '🛠️',
}

export function agentEmoji(kind: string): string {
  return AGENT_EMOJI[kind.toLowerCase()] ?? '🤖'
}

/** 头像底色：按名字哈希取固定色板，保证同一成员颜色稳定 */
const AVATAR_COLORS = ['#4d6bfe', '#8b5cf6', '#0ea5e9', '#f59e0b', '#10b981', '#ef4444', '#ec4899']

export function avatarColor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return AVATAR_COLORS[h % AVATAR_COLORS.length]
}

export const STATUS_META: Record<string, { dot: string; label: string }> = {
  working: { dot: 'gc-dot--working', label: '工作中' },
  blocked: { dot: 'gc-dot--blocked', label: '待处理' },
  idle: { dot: 'gc-dot--idle', label: '空闲' },
  done: { dot: 'gc-dot--idle', label: '空闲' },
  stopped: { dot: 'gc-dot--idle', label: '已停止' },
  unknown: { dot: 'gc-dot--idle', label: '未知' },
}

export function statusMeta(status: string): { dot: string; label: string } {
  return STATUS_META[status] ?? STATUS_META.unknown
}

export type PermissionMode = 'ask' | 'yolo' | 'auto'

export const KIMI_MODELS = [
  'kimi-code/k3',
  'kimi-code/kimi-for-coding',
  'kimi-code/kimi-for-coding-highspeed',
  'kimi-code/k3-256k',
] as const

/** 启动参数：模型走 -m；Kimi 权限走 -y / --auto。 */
export function buildLaunchArgs(
  kind: string,
  model = '',
  permission: PermissionMode = 'ask',
): string {
  const parts: string[] = []
  const trimmed = model.trim()
  if (trimmed) parts.push('-m', trimmed)
  if (kind === 'kimi') {
    if (permission === 'yolo') parts.push('-y')
    if (permission === 'auto') parts.push('--auto')
  }
  return parts.join(' ')
}

export function recallNotice(original: string): string {
  const clipped = original.trim().slice(0, 240)
  return clipped ? `【撤回】请忽略上一条消息：${clipped}` : '【撤回】请忽略上一条消息。'
}

export function canRecallEntry(ts: number, now = Date.now(), windowMs = 10 * 60 * 1000): boolean {
  return now - ts >= 0 && now - ts <= windowMs
}

export const COMPOSER_SKILLS = [
  { id: 'herdr', label: 'herdr', insert: '请按 herdr skill 操作终端 / pane。' },
  { id: 'mail', label: 'Agent Mail', insert: '请用 mail-recv --unread 领取；给 Leader 写信用 --to leader --thread cockpit。' },
] as const

export function attachMarkup(filename: string, path: string): string {
  const name = filename.trim() || '附件'
  const dest = path.trim()
  return dest ? `📎 ${name}\n${dest}` : `📎 ${name}`
}

export function appendAttachMarkup(current: string, filename: string, path: string): string {
  const block = attachMarkup(filename, path)
  const body = current.trimEnd()
  return body ? `${body}\n\n${block}\n` : `${block}\n`
}

export function clipboardImageFile(
  items: Array<{ type: string; getAsFile: () => File | null }>,
): File | null {
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) return file
    }
  }
  return null
}

export interface MailMessage {
  id: string | number
  sender: string
  program: string
  text: string
  to: string[]
  thread?: string
  ts: number
}

export function mailTimestamp(ts: number): number {
  if (!Number.isFinite(ts) || ts <= 0) return 0
  return ts < 1_000_000_000_000 ? ts * 1000 : ts
}

export function isHumanSender(sender: string): boolean {
  const name = sender.trim().toLowerCase()
  return (
    name === 'human' ||
    name === 'boss' ||
    name === '我' ||
    name === 'humanoverseer' ||
    name === 'overseer'
  )
}

/** 瀑布流收件人：human→我，program-main→唯一同 kind 成员花名。 */
export function displayReplyTargets(to: string[], members: ChatMember[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const raw of to) {
    if (!raw) continue
    let label = raw
    if (isHumanSender(raw)) {
      label = '我'
    } else {
      const member = members.find((item) => item.mailName === raw || item.name === raw)
      if (member) {
        label = member.name
      } else {
        const kind = raw.toLowerCase().replace(/-main$/, '').replace(/-agent-.*$/, '')
        const hits = members.filter((item) => item.kind === kind)
        if (hits.length === 1) label = hits[0].name
      }
    }
    const key = label.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(label)
  }
  return out
}

/** Agent 回给其他成员的花名；只回 Boss 时为空。 */
export function agentReplyTargets(to: string[], members: ChatMember[]): string[] {
  return displayReplyTargets(to, members).filter((name) => name !== '我')
}

export type MailEntry =
  | {
      id: string
      kind: 'me'
      text: string
      to: string[]
      mailTo: string[]
      ts: number
    }
  | {
      id: string
      kind: 'agent'
      paneId: string
      name: string
      agentKind: string
      isLeader: boolean
      text: string
      to: string[]
      ts: number
    }

/** Agent Mail 一封邮 → 瀑布流一条气泡。 */
export function mailToEntries(messages: MailMessage[], members: ChatMember[]): MailEntry[] {
  const out: MailEntry[] = []
  for (const message of messages) {
    const text = stripMailMeta(message.text)
    if (!text) continue
    const ts = mailTimestamp(message.ts)
    const rawId = String(message.id)
    const id = rawId.startsWith('pane:') || rawId.startsWith('mail:') ? rawId : `mail:${rawId}`
    const targets = displayReplyTargets(message.to, members)
    if (isHumanSender(message.sender)) {
      out.push({
        id,
        kind: 'me',
        text,
        to: targets,
        mailTo: message.to,
        ts,
      })
      continue
    }
    const member = members.find(
      (item) => item.mailName === message.sender || item.name === message.sender,
    )
    out.push({
      id,
      kind: 'agent',
      paneId: member?.paneId || (rawId.startsWith('pane:') ? rawId.slice(5) : `mail:${message.sender}`),
      name: member?.name || message.sender || 'agent',
      agentKind: member?.kind || message.program || 'agent',
      isLeader: member?.isLeader ?? false,
      text,
      to: targets,
      ts,
    })
  }
  return out
}

export function mailCoversLocalMe(
  mail: { kind: string; text: string }[],
  local: { text: string },
): boolean {
  return mail.some((item) => item.kind === 'me' && item.text === local.text)
}

const OVERSEER_PREAMBLE = /---\s*\n\s*🚨\s*MESSAGE FROM HUMAN OVERSEER 🚨[\s\S]*?The human's guidance supersedes all other priorities\.\s*\n\s*---\s*/giu
const BOSS_HINT = /^Boss 在群聊给你发了消息[^\n]*(?:\n+)(?:请用 mail-recv[^\n]*\n+)*/u
const META_COMMENT = /<!--\s*agent-cockpit-meta:[\s\S]*?-->\s*/gu
const COPIED_OVERSEER_CHROME = /(?:^|\n)@\S+\s*\nHumanOverseer\s*\nWebUI\s*\n\d{1,2}:\d{2}\s*\n---\s*\n/u

export function stripMailMeta(text: string): string {
  let out = text.replace(META_COMMENT, '')
  out = out.replace(OVERSEER_PREAMBLE, '\n')
  out = out.replace(BOSS_HINT, '')
  out = out.replace(COPIED_OVERSEER_CHROME, '\n')
  return out.replace(/\n{3,}/g, '\n\n').trim()
}

const LOCAL_ENTRIES_KEY = (session: string) => `gc:entries:v2:${session}`

export function loadLocalEntries(session: string): Array<Record<string, unknown>> {
  try {
    const raw = window.localStorage.getItem(LOCAL_ENTRIES_KEY(session))
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item) => item && typeof item === 'object') : []
  } catch {
    return []
  }
}

const COMPOSER_DRAFT_KEY = (session: string) => `gc:draft:${session}`

export function loadComposerDraft(session: string): string {
  try {
    return window.sessionStorage.getItem(COMPOSER_DRAFT_KEY(session)) || ''
  } catch {
    return ''
  }
}

export function saveComposerDraft(session: string, text: string): void {
  try {
    const key = COMPOSER_DRAFT_KEY(session)
    if (text) window.sessionStorage.setItem(key, text)
    else window.sessionStorage.removeItem(key)
  } catch {
    /* 隐私模式等降级 */
  }
}

export function saveLocalEntries(session: string, entries: Array<Record<string, unknown>>): void {
  try {
    const local = entries.filter((item) => {
      const kind = item.kind
      return kind === 'me' || kind === 'event' || kind === 'error'
    })
    window.localStorage.setItem(
      LOCAL_ENTRIES_KEY(session),
      JSON.stringify(local.slice(-300)),
    )
  } catch {
    /* 隐私模式等降级 */
  }
}

/** 会话聚合状态：blocked > working > idle > done（取最需要注意的） */
export function sessionAggregateStatus(members: ChatMember[]): string {
  const order = ['blocked', 'working', 'idle', 'done']
  for (const s of order) if (members.some((m) => m.status === s)) return s
  return 'unknown'
}

export function isBusyMember(member: ChatMember): boolean {
  return member.status === 'working' || member.status === 'blocked'
}

/** 工作中的占位气泡：一条状态，不是整屏 pane。 */
export function typingEntries(
  members: ChatMember[],
  now = Date.now(),
): Array<{
  id: string
  kind: 'agent'
  paneId: string
  name: string
  agentKind: string
  isLeader: boolean
  text: string
  to: string[]
  ts: number
}> {
  return members.filter(isBusyMember).map((member) => ({
    id: `typing:${member.paneId}`,
    kind: 'agent' as const,
    paneId: member.paneId,
    name: member.name,
    agentKind: member.kind,
    isLeader: member.isLeader,
    text: member.status === 'blocked' ? '需要你确认，点「看现场」处理' : '正在回复…',
    to: [],
    ts: now,
  }))
}

export function isLiveEntryId(id: string): boolean {
  return id.startsWith('pane:') || id.startsWith('typing:')
}

/** 切会话时不要把上一群的进出记进下一群。 */
export function shouldAnnounceMemberChange(
  prevSession: string | null,
  nextSession: string | null,
): boolean {
  return Boolean(nextSession) && prevSession === nextSession
}

/** 首次快照或名单还是空的：只记底，不刷「加入了群聊」。 */
export function shouldSeedMemberRoster(
  prevSession: string | null,
  nextSession: string | null,
  knownSize: number,
): boolean {
  return !shouldAnnounceMemberChange(prevSession, nextSession) || knownSize === 0
}

/** 成员进出是实时状态提示，不作为聊天历史跨刷新持久化。 */
export function isMemberRosterEvent(text: string): boolean {
  return /(?:加入了群聊|离开了群聊)$/.test(text.trim())
}

// ---------- 本地偏好 ----------

const FILES_VISIBLE_KEY = 'gc:files-visible'
const ACTIVE_SESSION_KEY = 'gc:active-session'

export function loadFilesVisible(): boolean {
  try {
    return window.localStorage.getItem(FILES_VISIBLE_KEY) !== '0'
  } catch {
    return true
  }
}

export function saveFilesVisible(visible: boolean): void {
  try {
    window.localStorage.setItem(FILES_VISIBLE_KEY, visible ? '1' : '0')
  } catch {
    /* 降级：不记忆 */
  }
}

export function loadActiveSession(): string | null {
  try {
    return window.localStorage.getItem(ACTIVE_SESSION_KEY)
  } catch {
    return null
  }
}

export function saveActiveSession(session: string): void {
  try {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, session)
  } catch {
    /* 降级：不记忆 */
  }
}

export function clearActiveSession(): void {
  try {
    window.localStorage.removeItem(ACTIVE_SESSION_KEY)
  } catch {
    /* 降级：不记忆 */
  }
}

/** 删掉当前会话后落到哪一条；删的不是当前会话则保持原样。 */
export function nextSessionAfterRemoval(
  names: string[],
  removed: string,
  current: string | null,
): string | null {
  const remaining = names.filter((name) => name !== removed)
  if (current !== removed) return current
  return remaining[0] ?? null
}

/** URL 里的 session 已不在列表时不要再写回 state，否则会和清空互踢狂刷。 */
export function shouldAdoptUrlSession(
  urlSession: string | null,
  activeSession: string | null,
  knownNames: string[],
  initialized: boolean,
): boolean {
  if (!urlSession || urlSession === activeSession) return false
  if (initialized && !knownNames.includes(urlSession)) return false
  return true
}

// ---------- 会话列表行模型 ----------

export interface SessionRow {
  name: string
  status: string
  memberCount: number
  root: string | null // 归属的项目根目录；null = 未匹配到任何 root
}

export function buildSessionRows(
  sessions: HerdrSession[],
  snapshot: HerdrSnapshot | null,
  roots: string[] = [],
): SessionRow[] {
  return sessions
    .filter((s) => s.name !== 'default')
    .filter((s) => s.status === 'running' || s.status === 'stopped')
    .map((s) => {
      const members = membersOfSession(snapshot, s.name)
      return {
        name: s.name,
        status: s.status === 'stopped' ? 'stopped' : sessionAggregateStatus(members),
        memberCount: members.length,
        root: sessionRoot(snapshot?.panes ?? [], s.name, roots),
      }
    })
}

/** 账本工作区：侧栏分组只按 thread.herdr_session，不再用 cwd 前缀碰 file-roots。 */
export interface LedgerWorkspace {
  id: string
  path: string
  title: string
}

export interface LedgerThread {
  workspace_id: string
  herdr_session: string
}

export interface LedgerGroup {
  id: string
  root: string
  label: string
  removable: boolean
  rows: SessionRow[]
}

export function groupByLedger(
  rows: SessionRow[],
  workspaces: LedgerWorkspace[],
  threads: LedgerThread[],
): { groups: LedgerGroup[]; ungrouped: SessionRow[] } {
  const byName = new Map(rows.map((row) => [row.name, row]))
  const claimed = new Set<string>()
  const groups: LedgerGroup[] = workspaces.map((ws) => {
    const attached: SessionRow[] = []
    for (const th of threads) {
      if (th.workspace_id !== ws.id) continue
      const row = byName.get(th.herdr_session)
      if (!row) {
        claimed.add(th.herdr_session)
        attached.push({
          name: th.herdr_session,
          status: 'stopped',
          memberCount: 0,
          root: ws.path,
        })
        continue
      }
      claimed.add(row.name)
      attached.push({ ...row, root: ws.path })
    }
    return {
      id: ws.id,
      root: ws.path,
      label: ws.title || rootBase(ws.path),
      removable: true,
      rows: attached,
    }
  })
  const ungrouped = rows.filter((row) => !claimed.has(row.name)).map((row) => ({
    ...row,
    root: null,
  }))
  return { groups, ungrouped }
}
