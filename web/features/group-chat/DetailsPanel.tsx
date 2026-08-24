// details 栏（AppFrame 第三列）：tab 头（成员 | 文件）+ 关闭钮，内容
// 复用 MemberPanel / FilePanel（embedded 态）。tab 观感取自 dsh
// ConversationRoot（见 DetailsPanel.module.css 头注释）。

import { useAppFrame } from '../shell/AppFrame'
import { cx } from '../shell/cx'
import { IconCloseFill14 } from '../shell/icons'
import type { TeamMember } from '../team/model'
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
  // 团队话题成员：与本机会话 Agent roster 严格分开。
  teamTopic?: string | null
  teamMembers?: TeamMember[]
  teamMembersLoading?: boolean
  teamMembersError?: string | null
  teamCurrentMentionHandle?: string | null
  onTeamMention?: (member: TeamMember) => void
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
  teamTopic = null,
  teamMembers = [],
  teamMembersLoading = false,
  teamMembersError = null,
  teamCurrentMentionHandle = null,
  onTeamMention,
  fileRoot,
  onPreview,
}: DetailsPanelProps) {
  const { toggleDetails } = useAppFrame()
  const teamMode = !!teamTopic
  const activeTeamMembers = teamMembers.filter((member) => member.status === 'active')

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
          />
        )}
      </div>
    </div>
  )
}
