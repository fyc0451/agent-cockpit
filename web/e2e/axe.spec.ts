import { AxeBuilder } from '@axe-core/playwright'
import type { AxeResults } from 'axe-core'
import { expect, test } from '@playwright/test'
import { attachGates, expectGatesClean, stubApi } from './helpers'

function seriousOnly(violations: AxeResults['violations']) {
  return violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
}

function violationDetails(violations: AxeResults['violations']): string[] {
  return seriousOnly(violations).flatMap((violation) =>
    violation.nodes.map(
      (node) => `${violation.id} ${node.target.join(' ')}: ${node.failureSummary ?? ''}`,
    ),
  )
}

const PAGES: [name: string, hash: string, ready: string][] = [
  ['chat', '/#/chat', '设置'],
  ['settings', '/#/settings', '外观'],
  ['upgrade', '/#/settings?view=upgrade', '一键升级'],
]

for (const theme of ['light', 'dark'] as const) {
  for (const [name, hash, ready] of PAGES) {
    test(`axe：${theme} ${name} 无 serious/critical`, async ({ page }) => {
      const g = attachGates(page)
      await page.emulateMedia({ colorScheme: theme })
      await stubApi(page)
      await page.goto(hash)
      await expect(page.getByText(ready).first()).toBeVisible()
      if (name === 'settings') {
        await expect(page.getByRole('tab', { name: '外观' })).toBeVisible()
        await expect(page.getByRole('radio', { name: '跟随系统' })).toBeVisible()
        await expect(page.getByText('返回群聊')).toHaveCount(0)
        await expect(page.getByText('Harness / Runtime 与节点')).toHaveCount(0)
      }
      if (name === 'upgrade') {
        await expect(page.getByRole('tab', { name: '升级' })).toBeVisible()
        await expect(page.getByRole('button', { name: '一键升级' })).toBeVisible()
      }
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
      const results = await new AxeBuilder({ page }).analyze()
      expect(violationDetails(results.violations)).toEqual([])
      expectGatesClean(g)
    })
  }

  test(`axe：${theme} 390px settings 无 serious/critical`, async ({ page }) => {
    const g = attachGates(page)
    await page.emulateMedia({ colorScheme: theme })
    await stubApi(page)
    await page.setViewportSize({ width: 390, height: 800 })
    await page.goto('/#/settings')
    await expect(page.getByRole('tab', { name: '外观' })).toBeVisible()
    const results = await new AxeBuilder({ page }).analyze()
    expect(violationDetails(results.violations)).toEqual([])
    expectGatesClean(g)
  })
}
