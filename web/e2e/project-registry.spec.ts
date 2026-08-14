import { AxeBuilder } from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import {
  discoveryGitPayload,
  registerCreatedPayload,
  registryProjectsEmptyPayload,
  registryProjectsEmptyWritablePayload,
  registryProjectsWritablePayload,
  runtimeNodesMultiUsablePayload,
} from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

/** stubApi 按 path 匹配不分 method；登记 POST 需要与 GET 列表分流，最后注册优先，GET 走 fallback */
async function stubRegisterPost(page: Page, payload: unknown, status = 201) {
  await page.route('**/api/project-registry/projects', (route) => {
    if (route.request().method() === 'POST') {
      return route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(payload),
      })
    }
    return route.fallback()
  })
}

const discoveryPostOverride = { '/api/project-discovery': discoveryGitPayload }

test('E1 键盘整轮登记：空态 CTA → root → 目录 → 识别 → 改 slug → 提交 → 成功卡片', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/project-registry/projects': registryProjectsEmptyWritablePayload,
    ...discoveryPostOverride,
  })
  await stubRegisterPost(page, registerCreatedPayload)
  await page.goto('/#/projects')
  await expect(page.locator('[data-state="empty"]')).toBeVisible()

  // 全程键盘激活（focus + Enter）
  const cta = page.getByRole('button', { name: '选择代码目录' })
  await cta.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  await expect(dialog).toBeVisible()

  // 唯一可用 local 节点 → 位置步自动跳过，直接是代码位置列表
  const root = dialog.getByRole('button', { name: '代码' })
  await root.focus()
  await page.keyboard.press('Enter')
  const dir = dialog.getByRole('button', { name: /^alpha/ })
  await dir.focus()
  await page.keyboard.press('Enter')
  const probe = dialog.getByRole('button', { name: '识别所选目录' })
  await probe.focus()
  await page.keyboard.press('Enter')
  await expect(dialog.getByText('新 Git 项目')).toBeVisible()

  // Slug 收在高级选项里
  await dialog.getByText('高级选项').click()
  const slug = dialog.getByLabel('Slug')
  await slug.focus()
  await slug.fill('alpha-proj')
  const submit = dialog.getByRole('button', { name: '确认添加' })
  await submit.focus()
  await page.keyboard.press('Enter')
  await expect(dialog.getByText('登记成功')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '继续创建工作空间' })).toBeVisible()
  expectGatesClean(g)
})

test('E2 remote 节点 fail-closed：reason 可读，Enter 后 0 请求', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/project-registry/projects': registryProjectsEmptyPayload,
    // 多可用节点：位置步不被自动跳过，disabled 卡片照常渲染
    '/api/runtime-nodes': runtimeNodesMultiUsablePayload,
  })
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '选择代码目录' }).click()
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  const remote = dialog.locator('[aria-disabled="true"]', { hasText: '远程 GPU 节点' })
  await expect(remote).toBeVisible()
  const descId = await remote.getAttribute('aria-describedby')
  expect(descId).toBeTruthy()
  await expect(dialog.locator(`[id="${descId}"]`)).toContainText(/未接通|非 local|不可用/)

  const callsBefore = g.apiRequests.length
  await remote.focus()
  await page.keyboard.press('Enter')
  await page.keyboard.press('Space')
  expect(g.apiRequests.length).toBe(callsBefore)
  // 仍停留在节点选择步
  await expect(dialog.getByRole('button', { name: /本机/ })).toBeVisible()
  expectGatesClean(g)
})

test('E3 stale 路径：提交注入 409 → 「重新探测」可见 → 点击回目录步', async ({ page }) => {
  const g = attachGates(page, [
    { url: '/api/project-registry/projects', status: 409, method: 'POST' },
  ])
  await stubApi(page, {
    '/api/project-registry/projects': registryProjectsWritablePayload,
    ...discoveryPostOverride,
  })
  await stubRegisterPost(
    page,
    { error: { code: 'discovery_stale', message: '目录状态已变化', retryable: false } },
    409,
  )
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '添加项目' }).click()
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  // 唯一可用 local 节点 → 位置步自动跳过
  await dialog.getByRole('button', { name: '代码' }).click()
  await dialog.getByRole('button', { name: /^alpha/ }).click()
  await dialog.getByRole('button', { name: '识别所选目录' }).click()
  await expect(dialog.getByText('新 Git 项目')).toBeVisible()
  await dialog.getByRole('button', { name: '确认添加' }).click()
  const reProbe = dialog.getByRole('button', { name: '重新探测' })
  await expect(reProbe).toBeVisible()
  await reProbe.click()
  // 回到目录步
  await expect(dialog.getByRole('button', { name: /^alpha/ })).toBeVisible()
  expectGatesClean(g)
})

test('E4 活机负断言（模拟）：registry 端点 404 → typed error，非白屏非 empty', async ({ page }) => {
  const g = attachGates(page, [{ url: '/api/project-registry/projects', status: 404 }])
  await stubApi(page, {
    // 模拟 18790 现状：端点不存在 → 404 envelope
    '/api/project-registry/projects': {
      __status: 404,
      __payload: { error: { code: 'not_found', message: 'registry endpoint missing', retryable: false } },
    },
  })
  await page.goto('/#/projects')
  await expect(page.locator('[data-state="error"]')).toBeVisible()
  await expect(page.getByText(/registry endpoint missing/)).toBeVisible()
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  await expect(page.locator('.page-title')).toHaveText('项目')
  expectGatesClean(g)
})

const WIDTHS = [1280, 1440, 1728, 860, 390] as const

for (const width of WIDTHS) {
  test(`E5 viewport ${width}px：列表与向导无横滚`, async ({ page }) => {
    const g = attachGates(page)
    await stubApi(page)
    await page.setViewportSize({ width, height: 800 })
    await page.goto('/#/projects')
    await expect(page.locator('.page-title')).toHaveText('项目')
    const noOverflow = async () => {
      const { sw, cw } = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        cw: document.documentElement.clientWidth,
      }))
      expect(sw).toBeLessThanOrEqual(cw + 1)
    }
    await noOverflow()
    await page.getByRole('button', { name: '添加项目' }).click()
    const dialog = page.getByRole('dialog', { name: '添加项目' })
    await expect(dialog).toBeVisible()
    await noOverflow()
    if (width === 390) {
      // 触控目标 ≥44px
      const addBtn = page.getByRole('button', { name: '添加项目' })
      const box = await addBtn.boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
      // 唯一可用 local 节点自动跳过 → 首个可点是代码位置
      const rootBtn = dialog.getByRole('button', { name: '代码' })
      const rootBox = await rootBtn.boundingBox()
      expect(rootBox!.height).toBeGreaterThanOrEqual(44)
    }
    expectGatesClean(g)
  })
}

test('E6 axe：列表 empty、向导三步、成功卡片无 serious/critical', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/project-registry/projects': registryProjectsEmptyWritablePayload,
    ...discoveryPostOverride,
  })
  await stubRegisterPost(page, registerCreatedPayload)
  await page.goto('/#/projects')
  // color-contrast 关闭理由同 axe.spec.ts：semantic tokens 为冻结设计值
  const run = () => new AxeBuilder({ page }).disableRules(['color-contrast']).analyze()
  const clean = (violations: Awaited<ReturnType<typeof run>>['violations']) =>
    violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')

  expect(clean((await run()).violations)).toEqual([]) // empty 列表
  await page.getByRole('button', { name: '选择代码目录' }).click()
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  expect(clean((await run()).violations)).toEqual([]) // 代码位置列表（唯一可用节点自动跳过位置步）
  await dialog.getByRole('button', { name: '代码' }).click()
  await expect(dialog.getByRole('button', { name: /^alpha/ })).toBeVisible()
  expect(clean((await run()).violations)).toEqual([]) // 目录步
  await dialog.getByRole('button', { name: /^alpha/ }).click()
  await dialog.getByRole('button', { name: '识别所选目录' }).click()
  await expect(dialog.getByText('新 Git 项目')).toBeVisible()
  expect(clean((await run()).violations)).toEqual([]) // 识别结果
  await dialog.getByRole('button', { name: '确认添加' }).click()
  await expect(dialog.getByText('登记成功')).toBeVisible()
  expect(clean((await run()).violations)).toEqual([]) // 成功卡片
  expectGatesClean(g)
})
