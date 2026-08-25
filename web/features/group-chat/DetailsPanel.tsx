// details 栏（AppFrame 第三列）：tab 头（成员 | 文件）+ 关闭钮，内容
// 复用 MemberPanel / FilePanel（embedded 态）。tab 观感取自 dsh
// ConversationRoot（见 DetailsPanel.module.css 头注释）。

import { useEffect, useMemo, useState } from 'react'
import { useAppFrame } from '../shell/AppFrame'
import { cx } from '../shell/cx'
import { IconCloseFill14 } from '../shell/icons'
import type { TeamBinding, TeamConsultCandidate, TeamMember } from '../team/model'
import type { ChatMember } from './model'
import { FilePanel } from './FilePanel'
import { MemberPanel } from './MemberPanel'
import css from './DetailsPanel.module.css'

export type DetailsTab = 'members' | 'files'

function teamPresenceLabel(member: TeamMember, now = Date.now()): string {
  if (member.online === true) return '在线'
  if (!member.last_seen_at) return '离线'
  const lastSeen = new Date(member.last_seen_at).getTime()
  if (!Number.isFinite(lastSeen)) return '离线'
  const elapsedMinutes = Math.max(0, Math.floor((now - lastSeen) / 60_000))
  if (elapsedMinutes < 1) return '刚刚在线'
  if (elapsedMinutes < 60) return `${elapsedMinutes} 分钟前`
  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) return `${elapsedHours} 小时前`
  return `${Math.floor(elapsedHours / 24)} 天前`
}

const TEAM_AGENT_KINDS = ['codex', 'claude', 'kimi', 'grok'] as const
type TeamAgentKind = (typeof TEAM_AGENT_KINDS)[number]

function teamAgentStatusLabel(status: string | null | undefined): string {
  if (status === 'working' || status === 'running') return '工作中'
  if (status === 'blocked') return '等待处理'
  if (status === 'idle' || status === 'done') return '空闲'
  if (status === 'stopped') return '已停止'
  return '状态未知'
}

function teamContextFreshnessLabel(
  context: NonNullable<TeamBinding['context']>,
  now = Date.now(),
): string {
  if (context.freshness === 'unavailable') return '不可用'
  const observed = context.observedAt ? new Date(context.observedAt).getTime() : Number.NaN
  if (!Number.isFinite(observed)) {
    if (context.freshness === 'partial') return '部分可用'
    return '时间未知'
  }
  const elapsedSeconds = Math.max(0, Math.floor((now - observed) / 1_000))
  let age: string
  if (elapsedSeconds < 15) {
    age = '刚刚同步'
  } else if (elapsedSeconds < 60) {
    age = `${elapsedSeconds} 秒前同步`
  } else if (elapsedSeconds < 300) {
    age = `${Math.floor(elapsedSeconds / 60)} 分钟前同步`
  } else {
    age = '已过期'
  }
  if (context.freshness === 'partial') return `部分可用 · ${age}`
  return age
}

function teamContextObservedLabel(value: string | null): string {
  if (!value) return '未知'
  const parsed = new Date(value)
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : '未知'
}

interface DetailsPanelProps {
  tab: DetailsTab
  onTabChange: (tab: DetailsTab) => void
  // 成员面板
  members: ChatMember[]
  membersLoading?: boolean
  session: string | null
  workdir: string | null
  onMention: (m: ChatMember) => void
  onFilter: (m: ChatMember) => void
  onInteract: (m: ChatMember) => void
  onOpenTerminal: () => void
  onMembersChanged: () => void
  externalAddSignal?: number
  availableAgentKinds?: readonly string[]
  // 团队话题成员：与本机会话 Agent roster 严格分开。
  teamTopic?: string | null
  teamMembers?: TeamMember[]
  teamMembersLoading?: boolean
  teamMembersError?: string | null
  teamCurrentMentionHandle?: string | null
  onTeamMention?: (member: TeamMember) => void
  teamBinding?: TeamBinding | null
  teamWorkspaces?: Array<{ id: string; label: string }>
  teamAvailableAgents?: readonly string[]
  teamConsultTargets?: TeamConsultCandidate[]
  onTeamCreateSession?: (projectSlug: string, input: {
    workspaceId: string
    agent: TeamAgentKind
    model?: string
    replyMode: 'confirm' | 'auto'
    replace?: boolean
  }) => Promise<void>
  onTeamDeleteSession?: (projectSlug: string) => Promise<void>
  onTeamSetConsultTarget?: (projectSlug: string, session: string | null) => Promise<void>
  // 文件面板：会话/项目目录；没有目录时不展示文件 tab
  fileRoot: string | null
  onPreview: (path: string) => void
}

export function DetailsPanel({
  tab,
  onTabChange,
  members,
  membersLoading = false,
  session,
  workdir,
  onMention,
  onFilter,
  onInteract,
  onOpenTerminal,
  onMembersChanged,
  externalAddSignal,
  availableAgentKinds,
  teamTopic = null,
  teamMembers = [],
  teamMembersLoading = false,
  teamMembersError = null,
  teamCurrentMentionHandle = null,
  onTeamMention,
  teamBinding = null,
  teamWorkspaces = [],
  teamAvailableAgents = [],
  teamConsultTargets = [],
  onTeamCreateSession,
  onTeamDeleteSession,
  onTeamSetConsultTarget,
  fileRoot,
  onPreview,
}: DetailsPanelProps) {
  const { toggleDetails } = useAppFrame()
  const teamMode = !!teamTopic
  const activeTeamMembers = teamMembers.filter((member) => member.status === 'active')
  const selectableTeamAgents = useMemo(
    () => TEAM_AGENT_KINDS.filter((agent) => teamAvailableAgents.includes(agent)),
    [teamAvailableAgents],
  )
  const [teamWorkspaceId, setTeamWorkspaceId] = useState(teamWorkspaces[0]?.id ?? '')
  const [teamAgentKind, setTeamAgentKind] = useState<TeamAgentKind>(selectableTeamAgents[0] ?? 'codex')
  const [teamAgentModel, setTeamAgentModel] = useState('')
  const [teamAgentReplyMode, setTeamAgentReplyMode] = useState<'confirm' | 'auto'>('confirm')
  const [teamAgentLoading, setTeamAgentLoading] = useState(false)
  const [teamAgentError, setTeamAgentError] = useState<string | null>(null)
  const [teamConsultSession, setTeamConsultSession] = useState('')

  useEffect(() => {
    if (!teamWorkspaces.some((workspace) => workspace.id === teamWorkspaceId)) {
      setTeamWorkspaceId(teamWorkspaces[0]?.id ?? '')
    }
  }, [teamWorkspaceId, teamWorkspaces])

  useEffect(() => {
    if (!selectableTeamAgents.includes(teamAgentKind)) {
      setTeamAgentKind(selectableTeamAgents[0] ?? 'codex')
    }
  }, [selectableTeamAgents, teamAgentKind])

  useEffect(() => {
    setTeamAgentReplyMode(teamBinding?.replyMode === 'auto' ? 'auto' : 'confirm')
    setTeamAgentError(null)
  }, [teamBinding?.replyMode, teamTopic])

  useEffect(() => {
    setTeamConsultSession(teamBinding?.consultTarget?.session ?? '')
  }, [teamBinding?.consultTarget?.session, teamTopic])

  const createTopicAgent = async () => {
    if (!teamTopic || !onTeamCreateSession) return
    if (!teamWorkspaceId) {
      setTeamAgentError('请先在本地添加该 Topic 对应的工作区')
      return
    }
    if (!selectableTeamAgents.includes(teamAgentKind)) {
      setTeamAgentError('请先在设置中启用 Codex、Claude、Kimi 或 Grok CLI')
      return
    }
    const replacing = !!teamBinding
    if (replacing && !window.confirm('将迁移为新的 Topic 专用 Agent；原绑定不再处理团队消息。继续？')) {
      return
    }
    setTeamAgentLoading(true)
    setTeamAgentError(null)
    try {
      await onTeamCreateSession(teamTopic, {
        workspaceId: teamWorkspaceId,
        agent: teamAgentKind,
        model: teamAgentModel.trim() || undefined,
        replyMode: teamAgentReplyMode,
        replace: replacing,
      })
      setTeamAgentModel('')
    } catch (error) {
      setTeamAgentError(error instanceof Error ? error.message : String(error))
    } finally {
      setTeamAgentLoading(false)
    }
  }

  const deleteTopicAgent = async () => {
    if (!teamTopic || !teamBinding?.managedRuntime || !onTeamDeleteSession) return
    if (!window.confirm(
      '删除这个 Topic 的本地 Agent？\n\n系统会停止并删除它的专用 Session；Topic、成员和消息历史都会保留。',
    )) return
    setTeamAgentLoading(true)
    setTeamAgentError(null)
    try {
      await onTeamDeleteSession(teamTopic)
    } catch (error) {
      setTeamAgentError(error instanceof Error ? error.message : String(error))
    } finally {
      setTeamAgentLoading(false)
    }
  }

  const saveConsultTarget = async () => {
    if (!teamTopic || !onTeamSetConsultTarget) return
    setTeamAgentLoading(true)
    setTeamAgentError(null)
    try {
      await onTeamSetConsultTarget(teamTopic, teamConsultSession || null)
    } catch (error) {
      setTeamAgentError(error instanceof Error ? error.message : String(error))
    } finally {
      setTeamAgentLoading(false)
    }
  }

  return (
    <div className={css.root}>
      <div className={css.tabs} role="tablist" aria-label="会话详情">
        <button
          type="button"
          role="tab"
          aria-selected={teamMode || tab === 'members'}
          className={cx(css.tab, (teamMode || tab === 'members') && css.tabActive)}
          onClick={() => { onTabChange('members') }}
        >
          成员
        </button>
        {!teamMode && fileRoot && (
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'files'}
            className={cx(css.tab, tab === 'files' && css.tabActive)}
            onClick={() => { onTabChange('files') }}
          >
            文件
          </button>
        )}
        <button
          type="button"
          className={css.close}
          aria-label="关闭详情栏"
          title="关闭详情栏"
          onClick={() => { toggleDetails() }}
        >
          <IconCloseFill14 size={14} />
        </button>
      </div>
      <div className={css.body}>
        {teamMode ? (
          <aside className="gc-members is-open" aria-label="团队成员">
            <details key={teamTopic} className={css.teamAgentDetails}>
              <summary className={css.teamAgentSummary}>
                <span className={css.teamAgentTitle}>我的 Topic Agent</span>
                <span className={css.teamAgentState}>
                  {teamBinding?.managedRuntime ? (
                    <>
                      {teamBinding.lead?.mailName || '本地 Agent'}
                      {' · '}{teamBinding.lead?.agent || 'Agent'}
                      {' · '}{teamAgentStatusLabel(teamBinding.lead?.status)}
                    </>
                  ) : (
                    teamBinding ? '旧绑定待迁移；普通本地会话不会再处理团队消息' : '尚未为这个 Topic 创建本地 Agent'
                  )}
                </span>
              </summary>
              {(onTeamCreateSession || (teamBinding?.managedRuntime && onTeamSetConsultTarget)) && (
                <div className={css.teamAgentForm}>
                  {teamBinding?.context && (
                    <div className={css.teamContext} aria-label="Topic 项目上下文">
                      <strong>
                        项目上下文：{teamContextFreshnessLabel(teamBinding.context)}
                      </strong>
                      <span>
                        SHA {teamBinding.context.sha?.slice(0, 8) || '未知'}
                        {teamBinding.context.dirty === true ? ' · 有未提交变更' : ''}
                        {teamBinding.context.dirty === false ? ' · 工作区干净' : ''}
                      </span>
                      <span>
                        handoff {teamBinding.context.handoffUpdated || '未知'}
                        {' · '}采样 {teamContextObservedLabel(teamBinding.context.observedAt)}
                      </span>
                    </div>
                  )}
                  {onTeamCreateSession && (<>
                    <select aria-label="Topic Agent 工作区" value={teamWorkspaceId} onChange={(event) => setTeamWorkspaceId(event.target.value)} disabled={teamAgentLoading}>
                      {teamWorkspaces.length === 0 && <option value="">没有本地工作区</option>}
                      {teamWorkspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.label}</option>)}
                    </select>
                    <select aria-label="Topic Agent 类型" value={teamAgentKind} onChange={(event) => setTeamAgentKind(event.target.value as TeamAgentKind)} disabled={teamAgentLoading || selectableTeamAgents.length === 0}>
                      {selectableTeamAgents.length === 0 && <option value="codex">没有可用 Agent CLI</option>}
                      {selectableTeamAgents.map((agent) => <option key={agent} value={agent}>{agent}</option>)}
                    </select>
                    <input aria-label="Topic Agent 模型" placeholder="模型（可选）" value={teamAgentModel} onChange={(event) => setTeamAgentModel(event.target.value)} disabled={teamAgentLoading} />
                    <select aria-label="Topic Agent 回复模式" value={teamAgentReplyMode} onChange={(event) => setTeamAgentReplyMode(event.target.value as 'confirm' | 'auto')} disabled={teamAgentLoading}>
                      <option value="confirm">确认后回复</option>
                      <option value="auto">自动回复</option>
                    </select>
                    <button type="button" onClick={() => void createTopicAgent()} disabled={teamAgentLoading || !teamWorkspaceId || selectableTeamAgents.length === 0}>
                      {teamAgentLoading ? '创建中…' : teamBinding ? '迁移 / 更换 Agent' : '创建 Topic Agent'}
                    </button>
                  </>)}
                  {teamAgentError && <div className="gc-modal-error">{teamAgentError}</div>}
                  {teamBinding?.managedRuntime && onTeamDeleteSession && (
                    <button
                      type="button"
                      className={css.teamAgentDelete}
                      onClick={() => void deleteTopicAgent()}
                      disabled={teamAgentLoading}
                    >
                      删除 Topic Agent
                    </button>
                  )}
                  {teamBinding?.managedRuntime && onTeamSetConsultTarget && (
                    <>
                      <label htmlFor="team-consult-target">缺少项目上下文时询问</label>
                      <select
                        id="team-consult-target"
                        aria-label="Topic Agent 咨询目标"
                        value={teamConsultSession}
                        onChange={(event) => setTeamConsultSession(event.target.value)}
                        disabled={teamAgentLoading}
                      >
                        <option value="">不启用咨询</option>
                        {teamConsultTargets.map((target) => (
                          <option key={target.session} value={target.session}>{target.label}</option>
                        ))}
                      </select>
                      {teamBinding.consultTarget && !teamBinding.consultTarget.ready && (
                        <div className="gc-modal-error">
                          {teamBinding.consultTarget.reason || '咨询目标不可用，需要重新选择'}
                        </div>
                      )}
                      <button
                        type="button"
                        onClick={() => void saveConsultTarget()}
                        disabled={teamAgentLoading}
                      >
                        保存咨询目标
                      </button>
                    </>
                  )}
                </div>
              )}
            </details>
            <div className="gc-members-head">
              <span>团队成员</span>
              <span className="gc-members-count">· {activeTeamMembers.length}</span>
            </div>
            <div className="gc-member-list">
              {teamMembersLoading && (
                <div className="gc-member-sub" style={{ padding: '8px 12px' }}>
                  成员加载中…
                </div>
              )}
              {teamMembersError && <div className="gc-modal-error">{teamMembersError}</div>}
              {!teamMembersLoading && !teamMembersError && activeTeamMembers.length === 0 && (
                <div className="gc-member-sub" style={{ padding: '8px 12px' }}>
                  该话题暂无成员
                </div>
              )}
              {activeTeamMembers.map((member) => {
                const name = member.display_name || `@${member.mention_handle}`
                const role = member.role === 'admin' ? '管理员' : '成员'
                const presence = teamPresenceLabel(member)
                const isCurrent = member.mention_handle.toLowerCase()
                  === teamCurrentMentionHandle?.toLowerCase()
                const content = (
                  <>
                    <span className="gc-member-avatar gc-member-avatar--human" aria-hidden>
                      {name.slice(0, 1).toUpperCase()}
                    </span>
                    <span className="gc-member-main">
                      <span className="gc-member-name">
                        <span
                          className={`gc-team-presence-dot${member.online ? ' is-online' : ''}`}
                          aria-hidden
                          title={presence}
                        />
                        {name}
                        {member.role === 'admin' && (
                          <span className="gc-leader-badge">管理员</span>
                        )}
                      </span>
                      <span className="gc-member-sub">
                        @{member.mention_handle} · {role} · {presence}
                      </span>
                      <span className="gc-member-sub">
                        Agent：{member.agent
                          ? `${member.agent.name || member.agent.kind || '未命名'} · ${teamAgentStatusLabel(member.agent.status)}`
                          : '未上报'}
                      </span>
                    </span>
                  </>
                )
                if (isCurrent || !onTeamMention) {
                  return (
                    <div key={member.human_id} className="gc-member gc-member--me">
                      {content}
                    </div>
                  )
                }
                return (
                  <button
                    key={member.human_id}
                    type="button"
                    className="gc-member"
                    aria-label={`将 @${member.mention_handle} 加入收件人`}
                    title={`发送给 @${member.mention_handle}`}
                    onClick={() => onTeamMention(member)}
                  >
                    {content}
                  </button>
                )
              })}
            </div>
          </aside>
        ) : tab === 'files' && fileRoot && session ? (
          <FilePanel
            session={session}
            root={fileRoot}
            open
            embedded
            onPreview={onPreview}
            onClose={() => { toggleDetails() }}
          />
        ) : (
          <MemberPanel
            members={members}
            loading={membersLoading}
            session={session}
            workdir={workdir}
            open
            onMention={onMention}
            onFilter={onFilter}
            onInteract={onInteract}
            onOpenTerminal={onOpenTerminal}
            onChanged={onMembersChanged}
            externalAddSignal={externalAddSignal}
            availableAgentKinds={availableAgentKinds}
          />
        )}
      </div>
    </div>
  )
}
