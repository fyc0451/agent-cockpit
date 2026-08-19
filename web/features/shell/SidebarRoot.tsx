// 侧栏外壳（折叠 rail + 新会话 + 工作区浏览区 + 设置 foot），取自
// @deepseek-ai/dsh-client-ui-sidebar 的 SidebarRoot (packages/client/
// ui-sidebar/src/client/SidebarRoot.tsx, MIT License)。折叠 = 滑动 +
// 交叉淡出：内容冻结在展开宽度（内联 style）原地淡出，滑动的列
// （AppFrame grid 轨道）裁剪它——中途不重排；落定后宽内容卸载，
// 上部控件从同一水平偏移进入 56px rail。原版的 Tooltip/品牌 wordmark
// 换成原生 title 与文字品牌。
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useAppFrame } from './AppFrame'
import { cx } from './cx'
import { IconNewChatOutline16, IconPanelLeftOutline16, IconSettingsOutline16 } from './icons'
import css from './SidebarRoot.module.css'

/** 宽内容卸载延迟；对齐 150ms 宽内容淡出。 */
const COLLAPSE_SETTLE_MS = 150

/** 指针离开列后滚动条保持绘制的时长（指针示能，不做离场即隐）。 */
const SCROLLBAR_LINGER_MS = 2000

export type SidebarRegion = ((wide: boolean) => ReactNode) | ReactNode

export interface SidebarRootProps {
  /** 「新会话」：无工作区时打开添加工作区，否则在当前工作区创建。 */
  onStartSession: () => void
  /** foot 设置入口。 */
  onOpenSettings: () => void
  /** 工作区浏览区（WorkspaceBrowser）；函数形态接收 wide 布局态。 */
  children?: SidebarRegion
}

/** 渲染侧栏列外壳（见模块注释）。 */
export function SidebarRoot({ onStartSession, onOpenSettings, children }: SidebarRootProps) {
  const { sidebarCollapsed: collapsed, sidebarWidth: width, toggleSidebar, narrow } = useAppFrame()

  // 折叠动画期间宽内容保持挂载（.fading 原地淡出），落定后卸载；
  // 展开立即重挂。
  const [settled, setSettled] = useState(collapsed)
  useEffect(() => {
    if (!collapsed) {
      setSettled(false)
      return
    }
    const timer = window.setTimeout(() => { setSettled(true) }, COLLAPSE_SETTLE_MS)
    return () => { window.clearTimeout(timer) }
  }, [collapsed])
  const wide = !collapsed || !settled

  // 淡出期间把内容冻结在展开宽度（collapsed && wide）：滑动的列裁剪
  // 而不是重排它。rail 布局（.collapsed 样式）在淡出落定后才生效。
  const lastWideWidth = useRef(width)
  if (!collapsed) lastWideWidth.current = width

  // rail 入场只交叉淡出真实折叠：冷启动直接 rail 态的渲染保持静态
  //（没有延迟隐藏的图标）。
  const everWide = useRef(!collapsed)
  if (!collapsed) everWide.current = true

  // 列内滚动条跟随指针：在列内绘制，离开后保留 SCROLLBAR_LINGER_MS。
  // 返回列内会取消待执行的隐藏，而不是从隐藏条重启。
  const column = useRef<HTMLDivElement>(null)
  const [pointerInside, setPointerInside] = useState(false)
  const lingerTimer = useRef<number | undefined>(undefined)
  const armLinger = (): void => {
    if (lingerTimer.current !== undefined) return
    lingerTimer.current = window.setTimeout(() => {
      lingerTimer.current = undefined
      setPointerInside(false)
    }, SCROLLBAR_LINGER_MS)
  }
  const cancelLinger = (): void => {
    window.clearTimeout(lingerTimer.current)
    lingerTimer.current = undefined
  }
  useEffect(() => cancelLinger, [])

  return (
    <div
      ref={column}
      className={cx(
        css.root,
        !wide && css.collapsed,
        !wide && everWide.current && css.railIn,
        collapsed && wide && css.fading,
        !pointerInside && css.quietBars,
      )}
      style={wide ? { width: collapsed ? lastWideWidth.current : width } : undefined}
      onPointerEnter={() => {
        cancelLinger()
        setPointerInside(true)
      }}
      onPointerLeave={() => { armLinger() }}
    >
      <div className={css.logoRow}>
        {/* 展开态品牌兼作新会话快捷方式；rail 态只有下方的展开钮。 */}
        {wide && (
          <button type="button" className={cx(css.brand, css.wide)} onClick={onStartSession} title="新会话">
            Agent Cockpit
          </button>
        )}
        <button
          type="button"
          className={css.iconButton}
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
          title={collapsed ? '展开侧栏' : '收起侧栏'}
          onClick={() => { toggleSidebar() }}
        >
          <IconPanelLeftOutline16 size={wide ? 16 : 18} />
        </button>
      </div>

      {/* 展开态按钮自带文字；rail 态只有图标 + title 提示。 */}
      <button
        type="button"
        className={css.newSession}
        aria-label="新会话"
        title={wide ? undefined : '新会话'}
        onClick={() => { onStartSession() }}
      >
        <IconNewChatOutline16 size={wide ? 14 : 18} />
        {wide && <span className={cx(css.newSessionLabel, css.wide)}>新会话</span>}
      </button>

      {/* 浏览区填充控件与 foot 之间的列空间；函数形态子元素拿到
          wide 布局态（淡出中间态保持宽内容，落定后切 rail）。 */}
      <div className={css.regionArea}>
        {typeof children === 'function' ? children(wide) : children}
      </div>

      <div className={css.footArea}>
        <button
          type="button"
          className={css.settings}
          onClick={() => {
            onOpenSettings()
            if (narrow && !collapsed) toggleSidebar()
          }}
          title={wide ? undefined : '设置'}
        >
          <IconSettingsOutline16 size={wide ? 16 : 18} />
          {wide && <span className={cx(css.settingsLabel, css.wide)}>设置</span>}
        </button>
      </div>
    </div>
  )
}
