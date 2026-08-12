import { expect, test } from '@playwright/test'
import { attachGates, expectGatesClean, stubApi } from './helpers'

test('WorkspaceSwitcher 键盘流：ArrowDown/Up roving、Enter 选中、Esc 恢复焦点（P2-10）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1')
  await expect(page.locator('.page-title')).toHaveText('本机工作区')

  const trigger = page.getByTitle('切换 Workspace')
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: 'Workspace 切换' })
  await expect(dialog).toBeVisible()

  const items = dialog.locator('.drawer-item')
  await expect(items).toHaveCount(2)

  // roving：第一项聚焦 → ArrowDown 到第二项（remote，disabled 但可聚焦）
  await items.first().focus()
  await page.keyboard.press('ArrowDown')
  await expect(items.nth(1)).toBeFocused()
  // Enter 在 disabled 项上被拦截：dialog 不关、URL 不变
  const hashBefore = page.url()
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  expect(page.url()).toBe(hashBefore)
  // ArrowUp 回到第一项，Enter 选中（跳同页 w1）→ dialog 关闭
  await page.keyboard.press('ArrowUp')
  await expect(items.first()).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(dialog).toHaveCount(0)
  expect(page.url()).toContain('/workspaces/w1')

  // 重新打开，Esc 关闭并恢复焦点到触发按钮
  await trigger.click()
  await expect(page.getByRole('dialog', { name: 'Workspace 切换' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Workspace 切换' })).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expectGatesClean(g)
})
