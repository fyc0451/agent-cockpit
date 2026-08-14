import { expect, test } from '@playwright/test'
import { REG_P1, workspaceDetailW1OpenPayload } from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

const WIDTHS = [1280, 1440, 1728, 860, 390] as const

for (const width of WIDTHS) {
  test(`viewport ${width}px：无水平溢出、导航可达`, async ({ page }) => {
    const g = attachGates(page)
    await stubApi(page)
    await page.setViewportSize({ width, height: 800 })
    await page.goto('/#/overview')
    await expect(page.locator('.page-title')).toHaveText('需要你处理')
    // 导航可达（rail 在任何宽度都可见——窄屏为图标列或底部横排）
    await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()

    const { sw, cw } = await page.evaluate(() => ({
      sw: document.documentElement.scrollWidth,
      cw: document.documentElement.clientWidth,
    }))
    expect(sw, `scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)

    await page.goto('/#/projects/p1/workbench')
    await expect(page.locator('.page-title')).toHaveText('Project One')
    const after = await page.evaluate(() => ({
      sw: document.documentElement.scrollWidth,
      cw: document.documentElement.clientWidth,
    }))
    expect(after.sw).toBeLessThanOrEqual(after.cw + 1)
    expectGatesClean(g)
  })
}

test('390px：触控目标 >= 44x44（P1-7）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.setViewportSize({ width: 390, height: 800 })
  await page.goto('/#/overview')
  await expect(page.locator('.page-title')).toHaveText('需要你处理')

  const rail = page.getByRole('navigation', { name: '主导航' })
  const railItem = rail.locator('.rail-item').first()
  await expect(railItem).toBeVisible()
  const railBox = await railItem.boundingBox()
  expect(railBox).not.toBeNull()
  expect(railBox!.width).toBeGreaterThanOrEqual(44)
  expect(railBox!.height).toBeGreaterThanOrEqual(44)

  // 顶栏按钮（主题 icon 按钮 + 搜索按钮）
  const iconBtn = page.locator('.topbar .btn--icon').first()
  const iconBox = await iconBtn.boundingBox()
  expect(iconBox!.width).toBeGreaterThanOrEqual(44)
  expect(iconBox!.height).toBeGreaterThanOrEqual(44)
  const searchBtn = page.locator('.topbar-search')
  const searchBox = await searchBtn.boundingBox()
  expect(searchBox!.height).toBeGreaterThanOrEqual(44)
  expectGatesClean(g)
})

test('390px：项目/文件/终端核心导航完整容纳且不扩大文档宽度', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.setViewportSize({ width: 390, height: 844 })

  for (const [hash, title] of [
    ['/#/projects/p1/workspaces/w1/files', '文件'],
    ['/#/projects/p1/workspaces/w1/terminal', '终端'],
  ] as const) {
    await page.goto(hash)
    await expect(page.locator('.page-title')).toHaveText(title)
    const rail = page.getByRole('navigation', { name: '主导航' })
    for (const label of ['项目', '文件', '终端']) {
      await expect(rail.getByRole('link', { name: label, exact: true })).toBeVisible()
    }
    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
      railScroll: document.querySelector('.rail-scroll')?.scrollWidth ?? 0,
      railClient: document.querySelector('.rail-scroll')?.clientWidth ?? 0,
    }))
    expect(widths.scroll).toBeLessThanOrEqual(widths.client + 1)
    expect(widths.railScroll).toBeLessThanOrEqual(widths.railClient + 1)
  }

  expectGatesClean(g)
})

test('Files 行内按钮（.btn-link）reset：无原生边框/背景、继承字体、左对齐占满行、可点进目录', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    // files.read 由 workspace detail meta 权威开启（默认世界关闭 → 页面向导态无行按钮）
    [`/api/project-registry/projects/${REG_P1}/workspaces/w1`]: workspaceDetailW1OpenPayload,
  })
  await page.goto('/#/projects/p1/workspaces/w1/files')
  await expect(page.locator('.page-title')).toHaveText('文件')
  const rowBtn = page.locator('.btn-link').first()
  await expect(rowBtn).toBeVisible()
  const probe = await rowBtn.evaluate((el) => {
    const cs = getComputedStyle(el)
    const row = el.closest('.list-row')
    return {
      borderStyle: cs.borderStyle,
      backgroundColor: cs.backgroundColor,
      textAlign: cs.textAlign,
      font: cs.fontFamily,
      parentFont: el.parentElement ? getComputedStyle(el.parentElement).fontFamily : '',
      width: el.getBoundingClientRect().width,
      rowWidth: row ? row.getBoundingClientRect().width : 0,
    }
  })
  expect(probe.borderStyle).toBe('none')
  expect(['rgba(0, 0, 0, 0)', 'transparent']).toContain(probe.backgroundColor)
  expect(['left', 'start']).toContain(probe.textAlign)
  expect(probe.font).toBe(probe.parentFont)
  // 占满列表行主区域（行 padding 28 + 右侧类型标签 ~30 以内）
  expect(probe.rowWidth - probe.width).toBeLessThanOrEqual(90)
  // 可点回归：点击目录行进入 src
  await rowBtn.click()
  await expect(page).toHaveURL(/path=src/)
  expectGatesClean(g)
})
