// details 栏（AppFrame 第三列）：tab 头（成员 | 文件）+ 关闭钮，内容
// 复用 MemberPanel / FilePanel（embedded 态）。tab 观感取自 dsh
// ConversationRoot（见 DetailsPanel.module.css 头注释）。

import { useAppFrame } from '../shell/AppFrame'
import { cx } from '../shell/cx'
import { IconCloseFill14 } from '../shell/icons'
import type { ChatMember } from './model'
import { FilePanel } from './FilePanel'
import { MemberPanel } from './MemberPanel'
import css from './DetailsPanel.module.css'

export type DetailsTab = 'members' | 'files'

interface DetailsPanelProps {
  tab: DetailsTab
  onTabChange: (tab: DetailsTab) => void
  // 成员面板
  members: ChatMember[]
  session: string | null
  workdir: string | null
  onMention: (m: ChatMember) => void
  onFilter: (m: ChatMember) => void
  onInteract: (m: ChatMember) => void
  onOpenTerminal: () => void
  onMembersChanged: () => void
  externalAddSignal?: number
  // 文件面板：会话/项目目录；没有目录时不展示文件 tab
  fileRoot: string | null
  onPreview: (path: string) => void
}

export function DetailsPanel({
  tab,
  onTabChange,
  members,
  session,
  workdir,
  onMention,
  onFilter,
  onInteract,
  onOpenTerminal,
  onMembersChanged,
  externalAddSignal,
  fileRoot,
  onPreview,
}: DetailsPanelProps) {
  const { toggleDetails } = useAppFrame()

  return (
    <div className={css.root}>
      <div className={css.tabs} role="tablist" aria-label="会话详情">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'members'}
          className={cx(css.tab, tab === 'members' && css.tabActive)}
          onClick={() => { onTabChange('members') }}
        >
          成员
        </button>
        {fileRoot && (
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
        {tab === 'files' && fileRoot && session ? (
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
