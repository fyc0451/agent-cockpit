import { AxeBuilder } from '@axe-core/playwright'
import type { AxeResults } from 'axe-core'
import { expect, test } from '@playwright/test'
import { attentionPayload, metaOk } from '../fixtures/api'
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
  ['overview', '/#/overview', '需要你处理'],
  ['workbench', '/#/projects/p1/workbench', 'Project One'],
  ['settings', '/#/settings', '设置'],
  ['files(forbidden)', '/#/projects/p1/workspaces/w1/files', '文件浏览暂不可用'],
  ['terminal', '/#/projects/p1/workspaces/w1/terminal', 'PTY 未接通'],
]

for (const theme of ['light', 'dark'] as const) {
  for (const [name, hash, ready] of PAGES) {
    test(`axe：${theme} ${name} 无 serious/critical`, async ({ page }) => {
      const g = attachGates(page)
      await page.emulateMedia({ colorScheme: theme })
      await stubApi(page)
      await page.goto(hash)
      await expect(page.getByText(ready).first()).toBeVisible()
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
      const results = await new AxeBuilder({ page }).analyze()
      expect(violationDetails(results.violations)).toEqual([])
      expectGatesClean(g)
    })
  }

  test(`axe：${theme} degraded 状态页无 serious/critical`, async ({ page }) => {
    const g = attachGates(page)
    await page.emulateMedia({ colorScheme: theme })
    await stubApi(page, {
      '/api/attention': {
        data: attentionPayload.data,
        meta: {
          ...metaOk,
          sources: [{ name: 'herdr', status: 'failed', observed_at: null, reason: 'Herdr 超时' }],
        },
      },
    })
    await page.goto('/#/overview')
    await expect(page.locator('[data-state="degraded"]').first()).toBeVisible()
    const results = await new AxeBuilder({ page }).analyze()
    expect(violationDetails(results.violations)).toEqual([])
    expectGatesClean(g)
  })

  test(`axe：${theme} 390px overview 无 serious/critical（P1-7）`, async ({ page }) => {
    const g = attachGates(page)
    await page.emulateMedia({ colorScheme: theme })
    await stubApi(page)
    await page.setViewportSize({ width: 390, height: 800 })
    await page.goto('/#/overview')
    await expect(page.locator('.page-title')).toHaveText('需要你处理')
    const results = await new AxeBuilder({ page }).analyze()
    expect(violationDetails(results.violations)).toEqual([])
    expectGatesClean(g)
  })
}
