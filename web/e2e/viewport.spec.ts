import { expect, test } from '@playwright/test'
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
