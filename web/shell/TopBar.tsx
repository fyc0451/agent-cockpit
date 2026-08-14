import { useSelection } from '../state/selection'
import { useTheme, type ThemePref } from '../state/theme'

const THEME_ICON: Record<ThemePref, string> = { system: '◐', light: '☼', dark: '☾' }
const THEME_LABEL: Record<ThemePref, string> = { system: '跟随系统', light: '亮色', dark: '暗色' }

export function TopBar({
  onOpenProjects,
  onOpenWorkspaces,
  onOpenPalette,
}: {
  onOpenProjects: () => void
  onOpenWorkspaces: () => void
  onOpenPalette: () => void
}) {
  const { projectSlug, workspaceId, project, workspace } = useSelection()
  const theme = useTheme()

  return (
    <header className="topbar">
      <button
        type="button"
        className="btn btn--ghost topbar-switcher"
        onClick={onOpenProjects}
        aria-haspopup="dialog"
        title="切换项目"
      >
        <span className="ellipsis topbar-switcher-name">
          {project?.name ?? projectSlug ?? '选择项目'}
        </span>
        {project?.branch ? <span className="topbar-branch ellipsis">{project.branch}</span> : null}
        <span aria-hidden="true">⌄</span>
      </button>

      {workspaceId ? (
        <button
          type="button"
          className="btn btn--ghost topbar-switcher"
          onClick={onOpenWorkspaces}
          aria-haspopup="dialog"
          title="切换工作空间"
        >
          <span
            className={`ws-dot ${workspace?.location === 'remote' ? 'ws-dot--remote' : 'ws-dot--local'}`}
            aria-hidden="true"
          />
          <span className="ellipsis topbar-switcher-name">{workspace?.name ?? workspaceId}</span>
          <span aria-hidden="true">⌄</span>
        </button>
      ) : null}

      <div className="topbar-spacer" />

      <button
        type="button"
        className="btn btn--secondary topbar-search"
        onClick={onOpenPalette}
        aria-haspopup="dialog"
        aria-label="搜索或运行命令"
        title="搜索或运行命令"
      >
        <span className="ellipsis">搜索或运行命令</span>
        <span className="topbar-search-icon" aria-hidden="true">⌕</span>
      </button>
      <button
        type="button"
        className="btn btn--icon"
        onClick={theme.cycle}
        title={`主题：${THEME_LABEL[theme.pref]}（点击切换）`}
        aria-label={`切换主题，当前${THEME_LABEL[theme.pref]}`}
      >
        <span aria-hidden="true">{THEME_ICON[theme.pref]}</span>
      </button>
      <span className="avatar" title="用户（占位）" aria-label="用户（占位）">
        C
      </span>
    </header>
  )
}
