import { expect, test } from '@playwright/test'
import { agentMailStatus } from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

test('loading：overview/attention 均未落定 → loading 态可见', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  // 后注册的路由优先：两个聚合 query 均永不响应
  await page.route('**/api/overview', () => {})
  await page.route('**/api/attention', () => {})
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="loading"]')).toBeVisible()
  expectGatesClean(g)
})

test('empty：真无数据 → empty 且无 degraded（负断言）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/attention': {
      sessions: [], items: [], count: 0, mail_unread: 0,
      capabilities: { agent_mail: agentMailStatus },
    },
    '/api/overview': {
      projects: [], total_unread: 0, total_projects: 0, total_agents: 0,
      agent_mail: agentMailStatus,
    },
  })
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="empty"]')).toBeVisible()
  await expect(page.getByText('还没有可汇总的工作')).toBeVisible()
  await expect(page.locator('[data-state="degraded"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('overview Agent Mail 不可用：真实 200 fallback 仍可渲染', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/overview': {
      projects: [], total_unread: 0, total_projects: 0, total_agents: 0,
      agent_mail: {
        available: false,
        reason: 'Agent Mail 查询失败',
        read_available: false,
        write_available: true,
        write_reason: null,
      },
    },
  })
  await page.goto('/#/overview')
  await expect(page.locator('.page-title')).toHaveText('需要你处理')
  await expect(page.locator('[data-state="empty"]')).toBeVisible()
  expectGatesClean(g)
})

test('partial-degraded：legacy attention 失败 → degraded，无 empty（负断言）', async ({ page }) => {
  const g = attachGates(page, [
    { url: '/api/attention', status: 500 },
    { url: '/api/attention', status: 500 },
    { url: '/api/attention', status: 500 },
  ])
  await stubApi(page, {
    '/api/attention': {
      __status: 500,
      __payload: { detail: '服务器内部错误，请稍后重试' },
    },
  })
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="degraded"]').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('待办摘要暂不可用')).toBeVisible()
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('legacy fetch abort → disconnected 态', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.route('**/api/overview', (route) => route.abort())
  await page.route('**/api/attention', (route) => route.abort())
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="disconnected"]')).toBeVisible({ timeout: 10_000 })
  await expect.poll(() => g.consoleErrors.length).toBe(6)
  const errors = g.consoleErrors
    .map(({ text, url }) => ({ text, path: new URL(url).pathname }))
    .sort((a, b) => a.path.localeCompare(b.path))
  const networkError = 'Failed to load resource: net::ERR_FAILED'
  expect(errors).toEqual([
    { text: networkError, path: '/api/attention' },
    { text: networkError, path: '/api/attention' },
    { text: networkError, path: '/api/attention' },
    { text: networkError, path: '/api/overview' },
    { text: networkError, path: '/api/overview' },
    { text: networkError, path: '/api/overview' },
  ])
  expect(g.apiFailures).toEqual([])
  expect(g.pageErrors).toEqual([])
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
})

test('legacy env-check 500：detail → error 态并重试', async ({ page }) => {
  const g = attachGates(page, [
    { url: '/api/env-check', status: 500 },
    { url: '/api/env-check', status: 500 },
    { url: '/api/env-check', status: 500 },
  ])
  await stubApi(page, {
    '/api/env-check': { __status: 500, __payload: { detail: '服务器内部错误，请稍后重试' } },
  })
  await page.goto('/#/settings?view=doctor')
  await expect(page.locator('[data-state="error"]')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByText('服务器内部错误，请稍后重试')).toBeVisible()
  expectGatesClean(g)
})

test('conflict：workbench 409 → conflict 态', async ({ page }) => {
  const g = attachGates(page, [{ url: '/api/projects/p1/workbench', status: 409 }])
  await stubApi(page, {
    '/api/projects/p1/workbench': {
      __status: 409,
      __payload: { detail: '项目兼容绑定冲突' },
    },
  })
  await page.goto('/#/projects/p1/workbench')
  await expect(page.locator('[data-state="conflict"]')).toBeVisible()
  expectGatesClean(g)
})

test('console gate：已知 workbench 409 不能掩盖未知 API 子路径 404', async ({ page }) => {
  const g = attachGates(page, [{ url: '/api/projects/p1/workbench', status: 409 }])
  await stubApi(page, {
    '/api/projects/p1/workbench': {
      __status: 409,
      __payload: { detail: '项目兼容绑定冲突' },
    },
  })
  await page.goto('/#/projects/p1/workbench')
  await expect(page.locator('[data-state="conflict"]')).toBeVisible()
  const status = await page.evaluate(() => fetch('/api/tasks/unexpected').then((r) => r.status))
  expect(status, '未知子路径不得前缀命中 /api/tasks fixture').toBe(404)
  await expect.poll(() => g.apiFailures.length).toBe(2)
  expect(() => expectGatesClean(g)).toThrow()
})

test('forbidden：files.read 关闭 → forbidden 态（负断言：无 empty）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/files')
  await expect(page.locator('[data-state="forbidden"]')).toBeVisible()
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  expectGatesClean(g)
})
