// 三栏让位链（concession chain）求解器与契约几何常量，取自
// @deepseek-ai/dsh-client-ui-layout (packages/client/ui-layout/src/client/
// columns.ts, MIT License, github.com/deepseek-ai/deepseek-harness)。
// 链序固定：先压 details 到下限，再自动收起 details（派生宽度，偏好
// 不改写，窗口变宽自动恢复），侧栏永不退让，中栏兜底吸收剩余赤字。

/** 一帧求解出的三栏宽度；中栏只在最终兜底时可能低于 CENTER_MIN。 */
export interface Columns {
  sidebar: number
  center: number
  details: number
}

/** 中栏下限；只有最终兜底可低于它。 */
export const CENTER_MIN = 640
/** 侧栏拖拽下限。 */
export const SIDEBAR_MIN = 264
/** 侧栏拖拽上限。 */
export const SIDEBAR_MAX = 420
/** 未拖拽时的侧栏宽度。 */
export const SIDEBAR_DEFAULT = 280
/** 收起侧栏不再占轨，只留左上角按钮。 */
export const SIDEBAR_COLLAPSED = 0
/** 低于该视口宽时侧栏自动收起；手动重开走覆盖层。 */
export const SIDEBAR_AUTO_COLLAPSE = 1024
/** 低于该宽度时 details 不再挤进栅格，改为覆盖层，否则手机点「成员」会被让位链关掉。 */
export const DETAILS_OVERLAY_BELOW = 860

export function shouldOverlayDetails(viewport: number): boolean {
  return viewport < DETAILS_OVERLAY_BELOW
}
/** details 拖拽下限。 */
export const DETAILS_MIN = 300
/** details 拖拽上限。 */
export const DETAILS_MAX = 520
/** 未拖拽时的 details 宽度。 */
export const DETAILS_DEFAULT = 300

/** 把面板宽度夹进契约区间。 */
export function clampWidth(px: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(px)))
}

/**
 * 对一个视口宽度求解三栏。纯函数、无迟滞：输出只随输入变化，
 * 窗口重新变宽时自动恢复偏好宽度。
 * @param viewport 可用帧宽（px）。
 * @param sidebar 侧栏宽度偏好（0 = 收起为 rail）。
 * @param details details 宽度偏好（0 = 关闭）。
 * @returns 求解结果；details 为 0 表示视觉关闭（子树不卸载），
 *          收起的侧栏宽度为 0。
 */
export function computeColumns(viewport: number, sidebar: number, details: number): Columns {
  const s = sidebar === 0 ? 0 : clampWidth(sidebar, SIDEBAR_MIN, SIDEBAR_MAX)
  const d0 = details === 0 ? 0 : clampWidth(details, DETAILS_MIN, DETAILS_MAX)

  // 第一步：偏好宽度全部放得下。
  if (s + d0 + CENTER_MIN <= viewport) return { sidebar: s, center: viewport - s - d0, details: d0 }

  // 第二步：压缩 details 到下限。
  const d1 = d0 === 0 ? 0 : Math.max(DETAILS_MIN, viewport - s - CENTER_MIN)
  if (s + d1 + CENTER_MIN <= viewport) return { sidebar: s, center: CENTER_MIN, details: d1 }

  // 第三步：自动关闭 details（派生，不改写偏好）；中栏吸收剩余赤字。
  return { sidebar: s, center: Math.max(0, viewport - s), details: 0 }
}
