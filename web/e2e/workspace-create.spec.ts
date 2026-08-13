// P0-WORKSPACE-001-F：创建 Workspace 纵切 E2E。
// 注意：后端 P0-WORKSPACE-001-B 并行实现中，本 spec 全部 page.route 夹具驱动（与
// local-slice.spec.ts 同惯例）；真实后端联调 E2E 待 B 合入后另行切片（需 seed
// Project + active/local/available RepoLocation，验收矩阵见 /tmp/p0-workspace001-claude/REPORT.md §5.1）。

import { expect, test, type Page } from '@playwright/test'
import {
  legacyWorkbenchPayload,
  metaOk,
  registryProjectsPayload,
} from '../fixtures/api'
import { apiCalls, attachGates, expectGatesClean, stubApi } from './helpers'

const alphaItem = registryProjectsPayload.data.items[0]
const ALPHA_ID = alphaItem.project.project_id
const ALPHA_LOC = alphaItem.repo_locations[0].repo_location_id
const WS_URL = `/api/project-registry/projects/${ALPHA_ID}/workspaces`

const createdWorkspace = {
  workspace_id: 'ws_new1',
  project_id: ALPHA_ID,
  repo_location_id: ALPHA_LOC,
  name: 'E2E 工作区',
  goal: null,
  isolation_kind: 'shared',
  lifecycle: 'active',
  active_run_id: null,
  version: 1,
  created_at: '2026-08-14T00:00:00+00:00',
  updated_at: '2026-08-14T00:00:00+00:00',
  repo_location: { node_id: 'local', availability: 'available' },
}

interface CreateCall {
  headers: Record<string, string>
  body: string
}

/** alpha workbench 世界 + POST 捕获（后注册优先于 stubApi 通配；非 POST 回退通配） */
async function stubAlphaWorld(
  page: Page,
  createResponse: { status: number; body: unknown },
): Promise<CreateCall[]> {
  const calls: CreateCall[] = []
  await stubApi(page, {
    '/api/projects/alpha/workbench': legacyWorkbenchPayload,
    [WS_URL]: { data: { items: [] }, meta: metaOk },
    [`${WS_URL}/ws_new1`]: { data: createdWorkspace, meta: metaOk },
  })
  await page.route(`**${WS_URL}`, (route) => {
    const req = route.request()
    if (req.method() !== 'POST') return route.fallback()
    calls.push({ headers: req.headers(), body: req.postData() ?? '' })
    return route.fulfill({
      status: createResponse.status,
      contentType: 'application/json',
      body: JSON.stringify(createResponse.body),
    })
  })
  return calls
}

async function expectNoHorizontalScroll(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

test('创建 Workspace happy path：严格 body + Idempotency-Key → 深链 workspace home', async ({
  page,
}) => {
  const g = attachGates(page)
  const calls = await stubAlphaWorld(page, {
    status: 201,
    body: { data: createdWorkspace, meta: metaOk },
  })
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/#/projects/alpha/workbench')
  await expect(page.getByText('暂无 Workspace')).toBeVisible()

  await page.getByRole('button', { name: '创建 Workspace' }).click()
  const dialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Workspace 名称').fill('E2E 工作区')
  await dialog.getByRole('button', { name: '确认创建' }).click()

  await expect(page).toHaveURL(new RegExp(`/#/projects/alpha/workspaces/ws_new1/files`))
  await expect(page.getByText('E2E 工作区').first()).toBeVisible()
  expect(calls).toHaveLength(1)
  expect(calls[0].headers['idempotency-key']).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  )
  expect(JSON.parse(calls[0].body)).toEqual({
    repo_location_id: ALPHA_LOC,
    name: 'E2E 工作区',
    goal: null,
    isolation_kind: 'shared',
  })
  await expectNoHorizontalScroll(page)
  expectGatesClean(g)
})

test('无合格 RepoLocation（p1）：按钮禁用 + reason 可见 + 点击零请求', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workbench')
  await expect(page.getByText('本机工作区').first()).toBeVisible()
  const btn = page.getByRole('button', { name: '创建 Workspace' })
  await expect(btn).toHaveAttribute('aria-disabled', 'true')
  await expect(btn).toHaveAttribute('title', /RepoLocation/)
  await btn.click({ force: true })
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(g.postRequests).toEqual([])
  expectGatesClean(g)
})

test('409 workspace_name_conflict：conflict 原地表达，不跳转', async ({ page }) => {
  const g = attachGates(page, [{ url: WS_URL, status: 409, method: 'POST' }])
  const calls = await stubAlphaWorld(page, {
    status: 409,
    body: {
      error: { code: 'workspace_name_conflict', message: '同名 Workspace 已存在', retryable: false },
    },
  })
  await page.goto('/#/projects/alpha/workbench')
  await expect(page.getByText('暂无 Workspace')).toBeVisible()
  await page.getByRole('button', { name: '创建 Workspace' }).click()
  const dialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await dialog.getByLabel('Workspace 名称').fill('E2E 工作区')
  await dialog.getByRole('button', { name: '确认创建' }).click()
  await expect(dialog.getByText('同名 Workspace 已存在').first()).toBeVisible()
  await expect(dialog).toBeVisible()
  expect(page.url()).toContain('/#/projects/alpha/workbench')
  expect(calls).toHaveLength(1)
  expectGatesClean(g)
})

test('窄屏 390：向导可用且无水平溢出；取消零 POST', async ({ page }) => {
  const g = attachGates(page)
  const calls = await stubAlphaWorld(page, {
    status: 201,
    body: { data: createdWorkspace, meta: metaOk },
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/projects/alpha/workbench')
  await expect(page.getByText('暂无 Workspace')).toBeVisible()
  await page.getByRole('button', { name: '创建 Workspace' }).click()
  const dialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Workspace 名称').fill('半途')
  await expectNoHorizontalScroll(page)
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(calls).toHaveLength(0)
  expect(g.postRequests).toEqual([])
  expectGatesClean(g)
})

test('legacy runtime 503：typed 显示不伪装空，Workspace 区块与创建入口仍可用', async ({
  page,
}) => {
  // retryable 503：初次 + 2 次 backoff 重试，共 3 次相同失败，需精确声明
  const wb503 = { url: '/api/projects/alpha/workbench', status: 503 }
  const g = attachGates(page, [wb503, wb503, wb503])
  const calls = await stubAlphaWorld(page, {
    status: 201,
    body: { data: createdWorkspace, meta: metaOk },
  })
  await page.route('**/api/projects/alpha/workbench', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Agent Mail 不可用' }),
    }),
  )
  await page.goto('/#/projects/alpha/workbench')
  // runtime typed 错误（retryable 有 backoff 重试，等待放宽）
  await expect(page.getByText('Agent Mail 不可用')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('暂无任务')).toHaveCount(0)
  // Workspace 区块独立可达，创建入口不受 legacy 故障影响
  await expect(page.getByText('暂无 Workspace')).toBeVisible()
  const btn = page.getByRole('button', { name: '创建 Workspace' })
  await expect(btn).not.toHaveAttribute('aria-disabled', 'true')
  await btn.click()
  const dialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '取消' }).click()
  expect(calls).toHaveLength(0)
  expect(g.postRequests).toEqual([])
  expect(apiCalls(g, '/api/files')).toEqual([])
  expectGatesClean(g)
})

test('wizard 在 workbench 之外零 legacy files 回退', async ({ page }) => {
  const g = attachGates(page)
  await stubAlphaWorld(page, {
    status: 201,
    body: { data: createdWorkspace, meta: metaOk },
  })
  await page.goto('/#/projects/alpha/workbench')
  await expect(page.getByText('暂无 Workspace')).toBeVisible()
  await page.getByRole('button', { name: '创建 Workspace' }).click()
  await page.getByRole('dialog', { name: '创建 Workspace' }).getByRole('button', { name: '取消' }).click()
  expect(apiCalls(g, '/api/files')).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})
