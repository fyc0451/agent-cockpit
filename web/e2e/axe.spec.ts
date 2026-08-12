import { AxeBuilder } from '@axe-core/playwright'
import type { AxeResults } from 'axe-core'
import { expect, test, type Page } from '@playwright/test'
import { attentionPayload, metaOk } from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

function seriousOnly(violations: AxeResults['violations']) {
  return violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
}

// color-contrast 关闭理由：--muted 等 semantic tokens 是产品摘要冻结值（W1 视觉冻结，
// 不得改动）；辅助文字 8–10px 低对比为冻结设计，对比度提升属设计侧后续议题。
function axe(page: Page) {
  return new AxeBuilder({ page }).disableRules(['color-contrast'])
}

const PAGES: [name: string, hash: string, ready: string][] = [
  ['overview', '/#/overview', '需要你处理'],
  ['workbench', '/#/projects/p1/workbench', 'Project One'],
  ['settings', '/#/settings', '设置'],
  ['files(forbidden)', '/#/projects/p1/workspaces/w1/files', '文件浏览暂不可用'],
  ['terminal', '/#/projects/p1/workspaces/w1/terminal', 'PTY 未接通'],
]

for (const [name, hash, ready] of PAGES) {
  test(`axe：${name} 无 serious/critical`, async ({ page }) => {
    const g = attachGates(page)
    await stubApi(page)
    await page.goto(hash)
    await expect(page.getByText(ready).first()).toBeVisible()
    const results = await axe(page).analyze()
    expect(
      seriousOnly(results.violations).map((v) => `${v.id}: ${v.nodes.length} nodes`),
    ).toEqual([])
    expectGatesClean(g)
  })
}

test('axe：degraded 状态页（九态代表）无 serious/critical', async ({ page }) => {
  const g = attachGates(page)
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
  const results = await axe(page).analyze()
  expect(seriousOnly(results.violations).map((v) => v.id)).toEqual([])
  expectGatesClean(g)
})

test('axe：390px viewport overview 无 serious/critical（P1-7）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.setViewportSize({ width: 390, height: 800 })
  await page.goto('/#/overview')
  await expect(page.locator('.page-title')).toHaveText('需要你处理')
  const results = await axe(page).analyze()
  expect(seriousOnly(results.violations).map((v) => v.id)).toEqual([])
  expectGatesClean(g)
})
