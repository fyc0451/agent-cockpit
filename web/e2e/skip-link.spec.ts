import { expect, test } from '@playwright/test'
import { attachGates, expectGatesClean, stubApi } from './helpers'

test('skip link：Tab 聚焦、Enter 聚焦 main，URL hash 不变（P1-2）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workbench')
  await expect(page.locator('.page-title')).toHaveText('Project One')
  const hashBefore = await page.evaluate(() => location.hash)

  // 真实键盘路径：第一个 Tab 落在 skip link 上
  await page.keyboard.press('Tab')
  await expect(page.locator('.skip-link')).toBeFocused()
  await page.keyboard.press('Enter')

  const hashAfter = await page.evaluate(() => location.hash)
  expect(hashAfter).toBe(hashBefore)
  const activeId = await page.evaluate(() => document.activeElement?.id)
  expect(activeId).toBe('main-content')
  // 页面内容未被路由变化打断
  await expect(page.locator('.page-title')).toHaveText('Project One')
  expectGatesClean(g)
})
