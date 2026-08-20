// 三栏外壳（侧栏 | 中栏 | details），取自 @deepseek-ai/dsh-client-ui-layout
// 的 AppFrame (packages/client/ui-layout/src/client/AppFrame.tsx, MIT
// License)：grid 轨道 + 拖拽 handle（pointer capture + rAF 节流）+
// 让位链求解（columns.ts）。原版的 cordis slot 体系换成直接 ReactNode
// props；列宽偏好持久化到 localStorage，侧栏/ details 栏通过 context
// 拿到自己的渲染态与切换回调。
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  clampWidth,
  computeColumns,
  DETAILS_DEFAULT,
  DETAILS_MAX,
  DETAILS_MIN,
  SIDEBAR_AUTO_COLLAPSE,
  SIDEBAR_DEFAULT,
  shouldOverlayDetails,
  SIDEBAR_MAX,
  SIDEBAR_MIN,
} from './columns'
import { IconPanelLeftOutline16 } from './icons'
import css from './AppFrame.module.css'

const SIDEBAR_STORAGE_KEY = 'cockpit.appframe.sidebar'
const DETAILS_STORAGE_KEY = 'cockpit.appframe.details'

/** 子栏（SidebarRoot 等）可消费的外壳状态。 */
export interface AppFrameState {
  /** 侧栏是否收起（窄视口自动收起 + 手动切换）。 */
  sidebarCollapsed: boolean
  /** 侧栏渲染宽度（收起为 0）。 */
  sidebarWidth: number
  toggleSidebar: () => void
  /** details 栏当前是否展开。 */
  detailsOpen: boolean
  toggleDetails: () => void
  /** 窄屏：侧栏是覆盖层，选会话后应收起。 */
  narrow: boolean
}

const FrameContext = createContext<AppFrameState | null>(null)

/** 在侧栏/details 子树内获取外壳布局状态。 */
export function useAppFrame(): AppFrameState {
  const state = useContext(FrameContext)
  if (state === null) throw new Error('useAppFrame 必须在 <AppFrame> 内使用')
  return state
}

function readStoredPref(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? n : fallback
  } catch {
    return fallback
  }
}

/**
 * 一条拖拽 handle：pointer capture，相对拖拽起点的 dx 以 rAF 节流上报。
 * `side` 决定悬停示能 CSS 归属的列。
 */
function DragHandle(props: {
  side: 'sidebar' | 'details'
  left: number
  onStart: () => void
  onDrag: (dx: number) => void
  onEnd: () => void
}) {
  const [dragging, setDragging] = useState(false)
  const origin = useRef(0)
  const latest = useRef(0)
  const frame = useRef<number | null>(null)
  const callbacks = useRef({ onStart: props.onStart, onDrag: props.onDrag, onEnd: props.onEnd })
  callbacks.current = { onStart: props.onStart, onDrag: props.onDrag, onEnd: props.onEnd }

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    origin.current = e.clientX
    latest.current = e.clientX
    callbacks.current.onStart()
    setDragging(true)
  }, [])
  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return
    latest.current = e.clientX
    frame.current ??= requestAnimationFrame(() => {
      frame.current = null
      callbacks.current.onDrag(latest.current - origin.current)
    })
  }, [])
  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return
    e.currentTarget.releasePointerCapture(e.pointerId)
    if (frame.current !== null) {
      cancelAnimationFrame(frame.current)
      frame.current = null
    }
    callbacks.current.onDrag(latest.current - origin.current)
    setDragging(false)
    callbacks.current.onEnd()
  }, [])

  return (
    <div
      className={css.handle}
      style={{ left: props.left }}
      data-side={props.side}
      data-dragging={dragging || undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    />
  )
}

export interface AppFrameProps {
  /** 侧栏内容（SidebarRoot；经 context 获知折叠态）。 */
  sidebar: ReactNode
  /** 中栏内容（群聊工作台）。 */
  children: ReactNode
  /** details 栏内容（成员/文件 tab）；关闭时子树保持挂载（宽度 0）。 */
  details?: ReactNode
  /** details 是否可用（如已有会话）；不可用时强制收起。 */
  detailsAvailable?: boolean
}

/** 三栏外壳（见模块注释）。 */
export function AppFrame({ sidebar, children, details, detailsAvailable = true }: AppFrameProps) {
  // 列宽偏好（0 = 收起/关闭），拖拽夹取在契约区间内。
  const [sidebarPref, setSidebarPref] = useState(() => readStoredPref(SIDEBAR_STORAGE_KEY, SIDEBAR_DEFAULT))
  const [detailsPref, setDetailsPref] = useState(() => readStoredPref(DETAILS_STORAGE_KEY, DETAILS_DEFAULT))
  // 窄视口下手动重开侧栏的覆盖位。
  const [narrowExpanded, setNarrowExpanded] = useState(false)
  const [overlayOpen, setOverlayOpen] = useState(false)
  const frameRef = useRef<HTMLDivElement | null>(null)
  const [viewport, setViewport] = useState(() => window.innerWidth)

  // 跟踪帧自身盒子（不是窗口）：rAF 节流的 ResizeObserver。
  // jsdom（vitest）没有 ResizeObserver，退化为忽略。
  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return
    const el = frameRef.current
    if (el === null) return
    let raf: number | null = null
    const observer = new ResizeObserver(() => {
      raf ??= requestAnimationFrame(() => {
        raf = null
        const width = el.getBoundingClientRect().width
        if (width > 0) setViewport(width)
      })
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      if (raf !== null) cancelAnimationFrame(raf)
    }
  }, [])

  // 偏好写回 localStorage。
  useEffect(() => {
    try { localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarPref)) } catch { /* ignore */ }
  }, [sidebarPref])
  useEffect(() => {
    try { localStorage.setItem(DETAILS_STORAGE_KEY, String(detailsPref)) } catch { /* ignore */ }
  }, [detailsPref])

  // 窄视口自动折叠：求解器保持无断点，窄屏重开走 narrowExpanded 覆盖，
  // 中栏吸收挤压。
  const narrow = viewport < SIDEBAR_AUTO_COLLAPSE
  const overlayDetails = shouldOverlayDetails(viewport)
  const overlayRef = useRef(overlayDetails)
  overlayRef.current = overlayDetails
  const wantDetails = detailsAvailable && detailsPref > 0
  const sidebarCollapsed = narrow ? !narrowExpanded : sidebarPref === 0
  const sidebarOverlay = narrow && !sidebarCollapsed
  const sidebarPreference = sidebarCollapsed || sidebarOverlay
    ? 0
    : (sidebarPref === 0 ? SIDEBAR_DEFAULT : sidebarPref)
  const cols = computeColumns(
    viewport,
    sidebarPreference,
    overlayDetails ? 0 : (wantDetails ? detailsPref : 0),
  )
  const detailsOpen = overlayDetails ? overlayOpen && detailsAvailable : cols.details > 0

  const toggleSidebar = useCallback(() => {
    if (narrow) setNarrowExpanded(v => !v)
    else setSidebarPref(p => (p === 0 ? SIDEBAR_DEFAULT : 0))
  }, [narrow])
  const toggleDetails = useCallback(() => {
    if (overlayRef.current) setOverlayOpen(open => !open)
    else setDetailsPref(p => (p === 0 ? DETAILS_DEFAULT : 0))
  }, [])

  useEffect(() => {
    if (!overlayDetails || !detailsOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') toggleDetails()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [overlayDetails, detailsOpen, toggleDetails])

  // 拖拽基点是拖拽开始时的渲染宽度（抓住被让位链夹过的面板不能跳回
  // 存储偏好）；整手势期间冻结，dx 不复利。
  const colsRef = useRef(cols)
  colsRef.current = cols
  const sidebarBase = useRef(0)
  const detailsBase = useRef(0)
  // 轨道缓停在整手势期间暂停：缓动的轨道会让列边脱离指针。
  const [dragging, setDragging] = useState(false)
  const onDragEnd = useCallback(() => { setDragging(false) }, [])
  const onSidebarStart = useCallback(() => { sidebarBase.current = colsRef.current.sidebar; setDragging(true) }, [])
  const onDetailsStart = useCallback(() => { detailsBase.current = colsRef.current.details; setDragging(true) }, [])
  const onSidebarDrag = useCallback((dx: number) => {
    setSidebarPref(clampWidth(sidebarBase.current + dx, SIDEBAR_MIN, SIDEBAR_MAX))
  }, [])
  const onDetailsDrag = useCallback((dx: number) => {
    setDetailsPref(clampWidth(detailsBase.current - dx, DETAILS_MIN, DETAILS_MAX))
  }, [])

  const overlaySidebarWidth = Math.min(320, Math.max(264, Math.round(viewport * 0.86)))
  const frameState = useMemo<AppFrameState>(() => ({
    sidebarCollapsed,
    sidebarWidth: sidebarCollapsed ? 0 : sidebarOverlay ? overlaySidebarWidth : cols.sidebar,
    toggleSidebar,
    detailsOpen,
    toggleDetails,
    narrow,
  }), [sidebarCollapsed, sidebarOverlay, overlaySidebarWidth, cols.sidebar, detailsOpen, toggleSidebar, toggleDetails, narrow])

  const focusMain = useCallback(() => {
    document.getElementById('main-content')?.focus()
  }, [])

  return (
    <FrameContext.Provider value={frameState}>
      <button type="button" className="skip-link" onClick={focusMain}>
        跳到主内容
      </button>
      <div
        ref={frameRef}
        className={css.frame}
        style={{
          gridTemplateColumns: sidebarCollapsed || sidebarOverlay
            ? `minmax(0, 1fr) ${cols.details}px`
            : `${cols.sidebar}px minmax(0, 1fr) ${cols.details}px`,
        }}
        data-sidebar-collapsed={sidebarCollapsed || undefined}
        data-details-collapsed={detailsOpen ? undefined : true}
        data-details-overlay={overlayDetails && detailsOpen ? true : undefined}
        data-dragging={dragging || undefined}
      >
        {!sidebarCollapsed && !sidebarOverlay && (
          <div className={css.sidebarCol}>{sidebar}</div>
        )}
        <div className={css.centerCol} id="main-content" tabIndex={-1}>
          {sidebarCollapsed && (
            <button
              type="button"
              className={css.sidebarFab}
              aria-label="打开侧栏"
              title="打开侧栏"
              onClick={toggleSidebar}
            >
              <IconPanelLeftOutline16 size={18} />
            </button>
          )}
          {children}
        </div>
        <div className={css.detailsCol}>{overlayDetails ? null : details}</div>
        {/* 折叠 rail 是定宽的：收起时不渲染 resize handle。 */}
        {!sidebarCollapsed && (
          <DragHandle side="sidebar" left={cols.sidebar} onStart={onSidebarStart} onDrag={onSidebarDrag} onEnd={onDragEnd} />
        )}
        {cols.details > 0 && !overlayDetails && (
          <DragHandle side="details" left={viewport - cols.details} onStart={onDetailsStart} onDrag={onDetailsDrag} onEnd={onDragEnd} />
        )}
        {sidebarOverlay && (
          <>
            <button
              type="button"
              className={css.sidebarBackdrop}
              aria-label="关闭侧栏"
              onClick={toggleSidebar}
            />
            <div className={css.sidebarOverlay}>{sidebar}</div>
          </>
        )}
        {overlayDetails && detailsOpen && (
          <>
            <button
              type="button"
              className={css.detailsBackdrop}
              aria-label="关闭成员栏"
              onClick={toggleDetails}
            />
            <div className={css.detailsOverlay}>{details}</div>
          </>
        )}
      </div>
    </FrameContext.Provider>
  )
}
