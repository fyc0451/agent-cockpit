import { expect, test, type Page } from '@playwright/test'
import {
  REG_P1,
  workspaceDetailW1OpenPayload,
  wsFileContentPayload,
  wsFilesRootPayload,
  wsFileSearchPayload,
  wsFilesSrcPayload,
} from '../fixtures/api'
import { apiCalls, attachGates, expectGatesClean, stubApi } from './helpers'

const WS_BASE = `/api/project-registry/projects/${REG_P1}/workspaces`

/** files.read 开启世界：detail meta 权威 capabilities + files 子树按 query 分流（后注册优先于 stubApi 通配） */
async function stubOpenWorld(page: Page) {
  await stubApi(page, { [`${WS_BASE}/w1`]: workspaceDetailW1OpenPayload })
  await page.route(`**${WS_BASE}/w1/files**`, (route) => {
    const u = new URL(route.request().url())
    const p = u.searchParams.get('path') ?? ''
    let payload: unknown
    if (u.pathname.endsWith('/files/content')) payload = wsFileContentPayload
    else if (u.pathname.endsWith('/files/search')) payload = wsFileSearchPayload
    else payload = p === 'src' ? wsFilesSrcPayload : wsFilesRootPayload
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payload),
    })
  })
}

async function expectNoHorizontalScroll(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

test('Files 深链冷启动与刷新：恢复同一 Project/Workspace/FileRef', async ({ page }) => {
  const g = attachGates(page)
  await stubOpenWorld(page)
  await page.goto('/#/projects/p1/workspaces/w1/files?path=src&file=README.md')
  await expect(page.getByText('main.ts', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: '文件预览 README.md' })).toBeVisible()
  await expect(page.getByText(/本机只读预览/)).toBeVisible()
  await page.reload()
  await expect(page.getByText('main.ts', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: '文件预览 README.md' })).toBeVisible()
  await expect(page.locator('.topbar')).toContainText('本机工作区')
  // 只读：全程零 POST、零 WebSocket、零 legacy /api/files
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
  expect(apiCalls(g, '/api/files')).toEqual([])
  expectGatesClean(g)
})

test('Files 目录导航/预览/搜索：query string 承载 FileRef', async ({ page }) => {
  const g = attachGates(page)
  await stubOpenWorld(page)
  await page.goto('/#/projects/p1/workspaces/w1/files')
  await expect(page.getByText('src/')).toBeVisible()
  await page.getByText('src/').click()
  await expect(page.getByText('main.ts', { exact: true })).toBeVisible()
  expect(page.url()).toContain('path=src')

  await page.getByLabel('搜索文件').fill('main')
  await page.getByRole('search').getByRole('button', { name: '搜索' }).click()
  await expect(page.getByRole('list', { name: '搜索 main 的结果' })).toBeVisible()
  expect(page.url()).toContain('q=main')

  // 键盘：结果项可聚焦并 Enter 打开预览
  await page.getByRole('list', { name: '搜索 main 的结果' }).getByRole('button').first().focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('region', { name: '文件预览 src/main.ts' })).toBeVisible()
  expectGatesClean(g)
})

test('Files cap=false：forbidden 可见，files 请求=0，POST=0，WS=0', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/files')
  await expect(page.locator('[data-state="forbidden"]')).toBeVisible()
  await expect(page.locator('.topbar')).toContainText('Project One')
  expect(g.apiRequests.filter((u) => u.includes('/files'))).toEqual([])
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})

test('Workspace mismatch：跨项目/未知 workspace → typed error，非 empty', async ({ page }) => {
  const g = attachGates(page, [{ url: `${WS_BASE}/w9`, status: 404 }])
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w9')
  await expect(page.getByText('Workspace 不存在或不属于当前项目')).toBeVisible()
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('Workbench source degraded：banner + sessions 降级，不假 empty', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/projects/p1/workbench': {
      project: { id: 7, slug: 'p1', created_at: '2026-08-01T00:00:00+00:00' },
      assignments: [],
      sessions: [],
      source: { available: false, degraded: true, observed_at: '2026-08-13T10:00:00+00:00' },
    },
  })
  await page.goto('/#/projects/p1/workbench')
  await expect(page.locator('[data-state="degraded"]').first()).toBeVisible()
  await expect(page.getByText('Session 列表不可用')).toBeVisible()
  await expect(page.getByText('暂无 session')).toHaveCount(0)
  expectGatesClean(g)
})

test('WorkspaceHome：terminal 卡 disabled 带冻结 reason，文件卡受 files.read 控制', async ({ page }) => {
  const g = attachGates(page)
  await stubOpenWorld(page)
  await page.goto('/#/projects/p1/workspaces/w1')
  await expect(page.locator('.page-title')).toHaveText('本机工作区')
  const main = page.locator('main')
  await expect(main.getByRole('link', { name: /文件/ })).toBeVisible()
  const terminalCard = main.locator('.card--disabled', { hasText: '终端' })
  await expect(terminalCard).toHaveAttribute('aria-disabled', 'true')
  await expect(terminalCard).toContainText('workspace_terminal_ticket_deferred')
  // 禁用卡激活零请求
  const before = g.apiRequests.length
  await terminalCard.dispatchEvent('click')
  expect(g.apiRequests.length).toBe(before)
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})

for (const width of [390, 860, 1280] as const) {
  test(`Files 页 viewport ${width}px：无水平溢出，导航可达`, async ({ page }) => {
    const g = attachGates(page)
    await stubOpenWorld(page)
    await page.setViewportSize({ width, height: 800 })
    await page.goto('/#/projects/p1/workspaces/w1/files?path=src')
    await expect(page.getByText('main.ts', { exact: true })).toBeVisible()
    await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
    await expectNoHorizontalScroll(page)
    await page.goto('/#/projects/p1/workspaces/w1/files?path=src&file=README.md')
    await expect(page.getByRole('region', { name: '文件预览 README.md' })).toBeVisible()
    await expectNoHorizontalScroll(page)
    expectGatesClean(g)
  })
}
