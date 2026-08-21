// 群聊工作台纯逻辑：成员模型、leader 持久化、@ 解析、摘要增量 diff、会话归属。
// 全部纯函数 / localStorage 存取，不依赖 React，便于 vitest 单测。

import type { HerdrPane, HerdrSession, HerdrSnapshot } from '../../api/legacyHerdr'

/** 可选的 agent 类型（codex / claude / kimi / opencode / grok / qodercli） */
export const AGENT_KINDS = ['codex', 'claude', 'kimi', 'opencode', 'grok', 'qodercli'] as const

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
  turnStartedMs?: number
  activity?: string
  unread?: number
}

export function leftoverMemberName(name: string, agent: string): boolean {
  const n = name.trim().toLowerCase()
  const a = agent.trim().toLowerCase()
  if (!n) return true
  return n === a || n === `${a}-main` || n.startsWith(`${a}-`)
}

function memberHasLeftoverName(member: ChatMember): boolean {
  const kind = member.kind.trim()
  return leftoverMemberName(member.name, kind) || leftoverMemberName(member.mailName, kind)
}

/** @kimi-main / @kimi-agent-* → 该类型唯一在场成员。多人同 kind 不猜。裸 @codex 只打 leftover 名，不打花名。 */
export function mentionMatchesLeftoverAlias(token: string, member: ChatMember, members: ChatMember[]): boolean {
  const kind = member.kind.trim().toLowerCase()
  if (!kind || !leftoverMemberName(token, kind)) return false
  if (members.filter((item) => item.kind.toLowerCase() === kind).length !== 1) return false
  if (token === kind) return memberHasLeftoverName(member)
  return true
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
      turnStartedMs: p.turn_started_ms,
      activity: p.activity,
      unread: p.unread,
    }
  })
}

/** 焦点必须正好落在本群一个 agent pane 上，才把终端输入记进瀑布流。空 shell 不记。 */
export function focusedMemberRecipient(
  snapshot: HerdrSnapshot | null,
  session: string,
): string | null {
  if (!snapshot || !session) return null
  const focused = snapshot.panes.filter((pane) => pane.session === session && pane.focused)
  if (focused.length !== 1 || !focused[0].agent) return null
  const member = membersOfSession(snapshot, session).find((item) => item.paneId === focused[0].pane_id)
  if (!member) return null
  return member.mailName || member.name || null
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

function leaderNameOf(member: ChatMember): string {
  return (member.mailName || member.name || '').trim()
}

/** 标记 leader：登记花名优先；没有才退回 localStorage / 第一个成员 */
export function withLeader(
  session: string,
  members: ChatMember[],
  registered?: { mail_name?: string } | null,
): ChatMember[] {
  if (members.length === 0) return members
  const registeredName = (registered?.mail_name || '').trim()
  const registeredHit = registeredName
    ? members.find((m) => {
        const name = leaderNameOf(m)
        return name.toLowerCase() === registeredName.toLowerCase()
      })
    : undefined
  const stored = loadLeaderPaneId(session)
  const storedHit = stored ? members.find((m) => m.paneId === stored) : undefined
  const leader = registeredHit || storedHit || members[0]
  if (leader.paneId !== stored) saveLeaderPaneId(session, leader.paneId)
  return members.map((m) => ({ ...m, isLeader: m.paneId === leader.paneId }))
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

const FOLD_LINES = 16
const FOLD_CHARS = 900
const PREVIEW_LINES = 12

export function stripAgentTuiFooter(raw: string): string {
  const lines = raw.replace(/\r\n/g, '\n').split('\n')
  while (lines.length) {
    const last = lines[lines.length - 1].trim()
    if (
      last.startsWith('›')
      || last.startsWith('> Improve ')
      || / · Full Access · /.test(last)
      || /Context \d+% left/.test(last)
    ) {
      lines.pop()
      continue
    }
    break
  }
  return lines.join('\n').trim()
}

const BOX_RULE = /^[\s┌┐└┘├┤┬┴┼╭╮╯╰━─┃│╔╗╚╝╠╣╦╩╬═]+$/
const BOX_RULE_MARK = /[┌┐└┘├┤┬┴┼╭╮╯╰━─╔╗╚╝╠╣╦╩╬═]/

function isBoxTableRow(line: string): boolean {
  const stripped = line.trim()
  const bars = (stripped.match(/[│┃]/g) || []).length
  return (stripped.startsWith('│') || stripped.startsWith('┃')) && bars >= 2
}

function isBoxTableRule(line: string): boolean {
  const stripped = line.trim()
  return stripped.length > 0 && BOX_RULE.test(stripped) && BOX_RULE_MARK.test(stripped)
}

function boxTableCells(line: string): string[] {
  return line.trim().replace(/^[│┃]/, '').replace(/[│┃]$/, '').replace(/┃/g, '│').split('│').map((cell) => cell.trim())
}

/** TUI 框线表 → Markdown 表，给瀑布流表格渲染用。 */
export function restoreBoxTables(raw: string): string {
  const lines = raw.split('\n')
  const out: string[] = []
  let index = 0
  while (index < lines.length) {
    if (isBoxTableRow(lines[index]) || isBoxTableRule(lines[index])) {
      const chunk: string[] = []
      while (index < lines.length) {
        const item = lines[index]
        if (!item.trim()) {
          if (chunk.length) {
            index += 1
            break
          }
          index += 1
          continue
        }
        if (isBoxTableRow(item) || isBoxTableRule(item)) {
          chunk.push(item)
          index += 1
          continue
        }
        break
      }
      const rows = chunk.filter(isBoxTableRow).map(boxTableCells)
      const width = rows[0]?.length ?? 0
      if (width >= 2 && rows.length >= 2 && rows.every((row) => row.length === width)) {
        out.push(`| ${rows[0].join(' | ')} |`)
        out.push(`| ${rows[0].map(() => '---').join(' | ')} |`)
        for (const row of rows.slice(1)) out.push(`| ${row.join(' | ')} |`)
        out.push('')
        continue
      }
      for (const item of chunk) {
        if (isBoxTableRow(item)) out.push(boxTableCells(item).join(' | '))
      }
      continue
    }
    out.push(lines[index])
    index += 1
  }
  return out.join('\n')
}

/** 终端把长路径从连字符或斜杠处折开时，拼回可点的一条。 */
export function joinBrokenFilePaths(raw: string): string {
  const lines = raw.split('\n')
  const out: string[] = []
  for (const line of lines) {
    const prev = out[out.length - 1]
    const lastToken = prev?.trimEnd().split(/\s+/).pop() || ''
    const cont = line.match(/^\s+([\w./\\-]+)(.*)$/)
    if (
      prev
      && cont
      && /[\/\\]/.test(lastToken)
      && /[-/\\]$/.test(lastToken)
      && !/https?:\/\//.test(lastToken)
      && /[\w./\\-]{2,}/.test(cont[1])
    ) {
      out[out.length - 1] = `${prev.replace(/\s+$/, '')}${cont[1]}${cont[2]}`
      continue
    }
    out.push(line)
  }
  return out.join('\n')
}

const FILE_PATH = /(?:~\/|\.\/|\/(?:home|mnt|Users|opt|var|tmp|usr|etc)\/|(?:[A-Za-z]:\\)|(?:[\w.-]+\/)+)[\w./\\-]*[\w.-]+\.[A-Za-z0-9]{1,12}/g

export function splitFilePaths(
  text: string,
): Array<{ type: 'text' | 'path'; text: string }> {
  const parts: Array<{ type: 'text' | 'path'; text: string }> = []
  const mark = new RegExp(FILE_PATH.source, 'g')
  let last = 0
  let match: RegExpExecArray | null
  while ((match = mark.exec(text))) {
    if (match.index > last) {
      parts.push({ type: 'text', text: text.slice(last, match.index) })
    }
    parts.push({ type: 'path', text: match[0] })
    last = match.index + match[0].length
  }
  if (last < text.length) parts.push({ type: 'text', text: text.slice(last) })
  return parts.filter((part) => part.text.length > 0)
}

/** 收获/粘贴挤成一行时，按分节和命令拆开，贴近终端排版。 */
export function reflowMessageText(raw: string): string {
  let text = stripAgentTuiFooter(raw.replace(/\r\n/g, '\n').trim())
  text = restoreBoxTables(text)
  text = joinBrokenFilePaths(text)
  text = text.replace(/^ {4,}/gm, '')
  text = text.replace(/^([^\n]{1,16}?)\s+(N\d{8,}\b.*)$/gm, '$1\n$2')
  const jammed = (text.match(/\n/g) || []).length < 2
  if (jammed) {
    text = text.replace(/([。；])\s*(一、|二、|三、|四、|五、|\d+\.\s)/g, '$1\n\n$2')
    text = text.replace(/([^\n])(一、|二、|三、|四、|五、)/g, '$1\n\n$2')
  }
  text = text.replace(/\s+(GET |POST |PUT |DELETE |uv run |docker |sudo )/g, '\n$1')
  return text.replace(/\n{3,}/g, '\n\n').trim()
}

export function isCommandLine(line: string): boolean {
  const trimmed = line.trim()
  return (
    /^(GET|POST|PUT|DELETE)\s+\//.test(trimmed)
    || /^(uv run |docker |sudo |herdr )/.test(trimmed)
    || /^\/(v1|healthz|api)\b/.test(trimmed)
    || /^\.\/[\w./-]+/.test(trimmed)
  )
}

export function isListLine(line: string): boolean {
  return /^(?:[-*•]|->|→)\s+\S/.test(line.trim())
}

export function splitSectionHeading(line: string): { title: string; rest: string } | null {
  const trimmed = line.trim()
  const hash = trimmed.match(/^(#{1,3})\s+(\S.*)$/)
  if (hash) return { title: hash[2], rest: '' }
  const numbered = trimmed.match(/^(\d+\.\s+[^\n：:]{1,40})$/)
  if (numbered) return { title: numbered[1], rest: '' }
  const labeled = trimmed.match(/^((?:\d+\.\s+|[一二三四五]、)[^\n：:]{1,40})[：:](.*)$/)
  if (labeled) return { title: labeled[1].trim(), rest: labeled[2].trim() }
  const tui = trimmed.match(/^([\u4e00-\u9fffA-Za-z0-9 ]{2,12})[：:](.+)$/)
  if (
    tui
    && trimmed.length <= 36
    && !/[=/]|--|花名|http/.test(trimmed)
    && !/[。！？.!?]$/.test(trimmed)
  ) {
    return { title: trimmed, rest: '' }
  }
  return null
}

export function splitInlineMarks(
  text: string,
): Array<{ type: 'text' | 'code' | 'strong' | 'path'; text: string }> {
  const parts: Array<{ type: 'text' | 'code' | 'strong' | 'path'; text: string }> = []
  const mark = /(\*\*[^*]+\*\*|`[^`]+`|\$\{[^}\n]+\})/g
  let last = 0
  let match: RegExpExecArray | null
  while ((match = mark.exec(text))) {
    if (match.index > last) {
      parts.push(...splitFilePaths(text.slice(last, match.index)))
    }
    if (match[0].startsWith('**')) {
      parts.push({ type: 'strong', text: match[0].slice(2, -2) })
    } else if (match[0].startsWith('${')) {
      parts.push({ type: 'code', text: match[0] })
    } else {
      parts.push({ type: 'code', text: match[0].slice(1, -1) })
    }
    last = match.index + match[0].length
  }
  if (last < text.length) parts.push(...splitFilePaths(text.slice(last)))
  return parts.filter((part) => part.text.length > 0)
}

export type LayoutBlock =
  | { type: 'heading'; text: string }
  | { type: 'text'; text: string }
  | { type: 'list'; items: string[] }
  | { type: 'code'; text: string }
  | { type: 'table'; headers: string[]; rows: string[][] }

function isTableSep(line: string): boolean {
  const trimmed = line.trim()
  if (/^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/.test(trimmed)) return true
  const cells = tableCells(trimmed)
  return cells.length >= 2 && cells.every((cell) => /^:?-{2,}:?$/.test(cell))
}

function isTableRow(line: string): boolean {
  const trimmed = line.trim()
  if (!trimmed.includes('|')) return false
  return trimmed.startsWith('|') || trimmed.split('|').length >= 3
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
}

function listItemText(line: string): string {
  return line.trim().replace(/^(?:[-*•]|->|→)\s+/, '')
}

/** 把已 reflow 的正文拆成终端式标题 / 列表 / 命令块。 */
export function layoutMessageBlocks(raw: string): LayoutBlock[] {
  const lines = reflowMessageText(raw).split('\n')
  const blocks: LayoutBlock[] = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }
    if (isTableRow(line) || isTableSep(line)) {
      const chunk: string[] = []
      while (index < lines.length && (isTableRow(lines[index]) || isTableSep(lines[index]))) {
        chunk.push(lines[index])
        index += 1
      }
      const body = chunk.filter((item) => !isTableSep(item)).map(tableCells)
        .filter((cells) => cells.some((cell) => !/^:?-{2,}:?$/.test(cell)))
      if (body.length >= 2) {
        blocks.push({ type: 'table', headers: body[0], rows: body.slice(1) })
        continue
      }
      if (body.length === 1) {
        blocks.push({ type: 'text', text: chunk.join('\n') })
        continue
      }
    }
    const heading = splitSectionHeading(line)
    if (heading) {
      blocks.push({ type: 'heading', text: heading.title })
      if (heading.rest) lines.splice(index + 1, 0, heading.rest)
      index += 1
      continue
    }
    if (isListLine(line)) {
      const items: string[] = []
      while (index < lines.length && isListLine(lines[index])) {
        items.push(listItemText(lines[index]))
        index += 1
      }
      blocks.push({ type: 'list', items })
      continue
    }
    if (isCommandLine(line)) {
      const chunk: string[] = []
      while (index < lines.length && isCommandLine(lines[index])) {
        chunk.push(lines[index].trim())
        index += 1
      }
      blocks.push({ type: 'code', text: chunk.join('\n') })
      continue
    }
    const para: string[] = []
    while (
      index < lines.length
      && lines[index].trim()
      && !splitSectionHeading(lines[index])
      && !isListLine(lines[index])
      && !isCommandLine(lines[index])
    ) {
      para.push(lines[index].replace(/\s+$/, ''))
      index += 1
    }
    blocks.push({ type: 'text', text: para.join('\n') })
  }
  return blocks
}

export function messageNeedsFold(text: string): boolean {
  const body = reflowMessageText(text)
  return body.split('\n').length > FOLD_LINES || body.length > FOLD_CHARS
}

export function messageFoldPreview(text: string, lines = PREVIEW_LINES): string {
  return reflowMessageText(text).split('\n').slice(0, lines).join('\n')
}

const CONCLUSION_HEAD = /^(?:[●•]\s+)?(?:===== .+ =====|对，你的判断|核心结论|结论如下|实现完成总结.*$|完成总结(?:[:：\s].*)?$|总结(?:[:：\s].*)?$|结论(?:[:：\s].*)?$|• 已查清|已查清，结论|处理方案[:：])/

/** 结论先露、过程另折。没有结论标题时 lead 是全文。 */
export function splitReplyPresentation(raw: string): { lead: string; rest: string } {
  const text = reflowMessageText(raw)
  if (!text) return { lead: '', rest: '' }
  const lines = text.split('\n')
  let start = -1
  for (let index = 0; index < lines.length; index += 1) {
    const stripped = lines[index].trim()
    if (CONCLUSION_HEAD.test(stripped) || stripped.includes('真正的回归')) {
      start = index
      break
    }
  }
  if (start < 0) return { lead: text, rest: '' }
  const before = lines.slice(0, start).join('\n').trim()
  const after = lines.slice(start).join('\n').trim()
  if (!before) return { lead: after, rest: '' }
  return { lead: after, rest: before }
}

export function composerPreviewLabel(value: string, empty = '写消息'): string {
  const text = value.replace(/\s+/g, ' ').trim()
  if (!text) return empty
  if (!messageNeedsFold(value) && text.length <= 80) return text
  const first = messageFoldPreview(value, 1).replace(/\s+/g, ' ').trim()
  const clipped = first.slice(0, 80)
  return clipped === text ? text : `${clipped}…`
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
  blocked: { dot: 'gc-dot--blocked', label: '等你输入' },
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

export function mailSkillInsert(session: string): string {
  const thread = session.trim() || 'cockpit'
  return (
    `结论写在终端，群聊会收进瀑布流。需要写信时用 mail-send --to leader --thread ${thread}，不要写 grok-main。`
  )
}

export const COMPOSER_SKILLS = [
  { id: 'herdr', label: 'herdr', insert: '请按 herdr skill 操作终端 / pane。' },
  { id: 'mail', label: 'Agent Mail', insert: mailSkillInsert('cockpit') },
] as const

export function composerSkills(session: string): Array<{ id: string; label: string; insert: string }> {
  return [
    COMPOSER_SKILLS[0],
    { id: 'mail', label: 'Agent Mail', insert: mailSkillInsert(session) },
  ]
}

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

export type ChatDelivery = 'interrupt' | 'queue'

export function normalizeChatDelivery(value: unknown): ChatDelivery | undefined {
  return value === 'queue' || value === 'interrupt' ? value : undefined
}

export function chatDeliveryLabel(delivery: ChatDelivery | undefined): string | null {
  if (delivery === 'interrupt') return '打断'
  if (delivery === 'queue') return '排队'
  return null
}

export type ChatReceipt = 'queued' | 'sent' | 'read'

export function chatReceiptOf(
  mailTo: string[],
  notifiedTo?: string[],
  readBy?: string[],
  delivery?: ChatDelivery,
): ChatReceipt | undefined {
  const dests = mailTo.filter(Boolean)
  if (dests.length === 0) return undefined
  const notified = new Set(notifiedTo || [])
  const read = new Set(readBy || [])
  if (dests.every((name) => read.has(name))) return 'read'
  if (dests.some((name) => notified.has(name))) return 'sent'
  if (delivery === 'queue' || delivery === 'interrupt') return 'queued'
  return undefined
}

export function chatReceiptLabel(receipt: ChatReceipt | undefined): string | null {
  if (receipt === 'queued') return '排队中'
  if (receipt === 'sent') return '已送达'
  if (receipt === 'read') return '已读'
  return null
}

export function formatChatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  const total = Math.max(0, Math.round(ms / 1000))
  if (total < 60) return `${total}秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  if (minutes < 60) return seconds > 0 ? `${minutes}分${seconds}秒` : `${minutes}分`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest > 0 ? `${hours}小时${rest}分` : `${hours}小时`
}

export function liveTurnLine(member: ChatMember, now = Date.now()): string {
  const elapsed = member.turnStartedMs
    ? formatChatDuration(now - member.turnStartedMs)
    : ''
  if (member.status === 'blocked') {
    return elapsed ? `等你输入 · 已 ${elapsed}` : '等你输入'
  }
  const activity = (member.activity || '').trim() || '正在回复'
  return elapsed ? `${activity} · ${elapsed}` : activity
}

export function unreadCountLabel(count: number | undefined): string | null {
  if (!count || count <= 0) return null
  return count > 99 ? '99+' : String(count)
}

export interface MailMessage {
  id: string | number
  sender: string
  program: string
  text: string
  to: string[]
  thread?: string
  ts: number
  delivery?: ChatDelivery
  notified_to?: string[]
  read_by?: string[]
  duration_ms?: number
  git?: { files: number; stat: string }
}

export function mailTimestamp(ts: number): number {
  if (!Number.isFinite(ts) || ts <= 0) return 0
  return ts < 1_000_000_000_000 ? ts * 1000 : ts
}

/** 当天只显示时分；隔日带月日，避免昨晚的气泡看起来像今早刚发。 */
export function formatChatClock(ts: number, now = Date.now()): string {
  const stamp = mailTimestamp(ts)
  if (!stamp) return ''
  const date = new Date(stamp)
  const current = new Date(now)
  const time = date.toLocaleTimeString('zh-CN', {
    hour12: false, hour: '2-digit', minute: '2-digit',
  })
  const sameDay = (
    date.getFullYear() === current.getFullYear()
    && date.getMonth() === current.getMonth()
    && date.getDate() === current.getDate()
  )
  if (sameDay) return time
  const day = date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  return `${day} ${time}`
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
      delivery?: ChatDelivery
      receipt?: ChatReceipt
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
      durationMs?: number
      unread?: number
      git?: { files: number; stat: string }
    }

/** Agent Mail 一封邮 → 瀑布流一条气泡。 */
export function mailToEntries(messages: MailMessage[], members: ChatMember[]): MailEntry[] {
  const out: MailEntry[] = []
  for (const message of messages) {
    const text = stripMailMeta(message.text)
    if (!text || isIdentityChromeOnly(text)) continue
    const ts = mailTimestamp(message.ts)
    const rawId = String(message.id)
    const id = rawId.startsWith('pane:') || rawId.startsWith('mail:') ? rawId : `mail:${rawId}`
    const targets = displayReplyTargets(message.to, members)
    if (isHumanSender(message.sender)) {
      if (message.to.length === 1 && message.to[0] === '终端') continue
      out.push({
        id,
        kind: 'me',
        text,
        to: targets,
        mailTo: message.to,
        ts,
        delivery: normalizeChatDelivery(message.delivery),
        receipt: chatReceiptOf(
          message.to, message.notified_to, message.read_by,
          normalizeChatDelivery(message.delivery),
        ),
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
      durationMs: typeof message.duration_ms === 'number' ? message.duration_ms : undefined,
      unread: member?.unread,
      git: message.git
        && typeof message.git.files === 'number'
        && typeof message.git.stat === 'string'
        ? { files: message.git.files, stat: message.git.stat }
        : undefined,
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
const BOSS_HINT = /^[ \t❯]*Boss 在群聊给你(?:发了|排了一条)消息[^\n]*(?:\n+[ \t]*(?:请直接做|请做完手头事|请用 mail-recv|结论写在终端|本群 Leader 是|给 Leader 写信|需要写信时|不要写 grok-main)[^\n]*)*/mu
const META_COMMENT = /<!--\s*agent-cockpit-meta:[\s\S]*?-->\s*/gu
const COPIED_OVERSEER_CHROME = /(?:^|\n)@\S+\s*\nHumanOverseer\s*\nWebUI\s*\n\d{1,2}:\d{2}\s*\n---\s*\n/u

const IDENTITY_CHROME = /协作通信约定|mail-recv|mail-send|--instance\s*main|codex-luna-agent-cockpit|注册:花名|\[agent-mail 身份|普通打断保存|停止\/转向不恢复|先 claim|complete\/ack|agent-mail-tools|--unread|--subject|--body|花名=|目=\/home\/|^home\/fyc\/|^codex --instance|^花名>|已知晓，身份|重复身份通知|通知已忽略|无新任务|hook exited|UserPromptS|--agent\s*codex|--instan|--projec|fyc\/github\/agent-cockpit/
const IDENTITY_DIAGNOSIS = /这不是|空转|瀑布流|旧身份|残稿|挖空|不是任务|没有任务结论|身份壳|leftover 壳/

function isIdentityChromeLine(line: string): boolean {
  if (IDENTITY_DIAGNOSIS.test(line)) return false
  return IDENTITY_CHROME.test(line)
}

export function isIdentityChromeOnly(text: string): boolean {
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean)
  if (lines.length === 0) return false
  const leftover = lines.filter((line) => !isIdentityChromeLine(line))
  if (leftover.length === 0) return true
  if (leftover.length === lines.length) return false
  return leftover.join(' ').length < 24
}

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
  unread?: number
}> {
  return members.filter(isBusyMember).map((member) => ({
    id: `typing:${member.paneId}`,
    kind: 'agent' as const,
    paneId: member.paneId,
    name: member.name,
    agentKind: member.kind,
    isLeader: member.isLeader,
    text: liveTurnLine(member, now),
    to: [],
    ts: now,
    unread: member.unread,
    waiting: member.status === 'blocked',
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

/**
 * 侧栏点了会话会先改 activeSession，URL 下一拍才跟上。
 * URL 没变时不要把刚点的会话踢回旧 query，否则主栏空白，刷新才出内容。
 */
export function shouldFollowUrlSession(
  urlSession: string | null,
  activeSession: string | null,
  knownNames: string[],
  initialized: boolean,
  urlChanged: boolean,
): boolean {
  if (!shouldAdoptUrlSession(urlSession, activeSession, knownNames, initialized)) {
    return false
  }
  if (!urlChanged && urlSession !== activeSession) return false
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
