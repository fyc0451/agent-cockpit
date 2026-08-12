import { expect, test } from '@playwright/test'
import { attentionPayload, metaOk } from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

test('loading：overview 永不落定 → loading 态可见', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  // 后注册的路由优先：overview 永不响应
  await page.route('**/api/overview', () => {})
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="loading"]')).toBeVisible()
  expectGatesClean(g)
})

test('empty：真无数据 → empty 且无 degraded（负断言）', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/attention': { data: { items: [] }, meta: metaOk },
    '/api/overview': { data: { projects: [] }, meta: metaOk },
  })
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="empty"]')).toBeVisible()
  await expect(page.getByText('还没有可汇总的工作')).toBeVisible()
  await expect(page.locator('[data-state="degraded"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('partial-degraded：source failed → degraded + 数据仍在，无 empty（负断言）', async ({ page }) => {
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
  await expect(page.getByText(/Herdr 超时/)).toBeVisible()
  await expect(page.getByText('ReviewPacket 待决定')).toBeVisible()
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('partial-degraded：overview source failed 不得被正常 attention meta 吞掉', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/overview': {
      data: { projects: [] },
      meta: {
        ...metaOk,
        partial: true,
        sources: [{ name: 'registry', status: 'failed', observed_at: null, reason: 'Registry 超时' }],
      },
    },
    '/api/attention': { data: { items: [] }, meta: metaOk },
  })
  await page.goto('/#/overview')
  await expect(page.getByText(/Registry 超时/)).toBeVisible()
  await expect(page.locator('[data-state="degraded"]')).toHaveCount(2)
  await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  expectGatesClean(g)
})

test('disconnected：transport_lost → disconnected 态', async ({ page }) => {
  const g = attachGates(page, [
    { url: '/api/overview', status: 502 },
    { url: '/api/attention', status: 502 },
  ])
  await stubApi(page, {
    '/api/overview': {
      __status: 502,
      __payload: { error: { code: 'transport_lost', message: '连接中断', retryable: false } },
    },
    '/api/attention': {
      __status: 502,
      __payload: { error: { code: 'transport_lost', message: '连接中断', retryable: false } },
    },
  })
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="disconnected"]')).toBeVisible()
  expectGatesClean(g)
})

test('stale：doctor 源 data_stale → stale 态', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.route('**/api/env-check', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'data_stale', message: '缓存过期', retryable: false } }),
    }),
  )
  await page.goto('/#/settings?view=doctor')
  await expect(page.locator('[data-state="stale"]')).toBeVisible()
  await expect(page.getByText('缓存过期')).toBeVisible()
  expectGatesClean(g)
})

test('conflict：workbench 409 → conflict 态', async ({ page }) => {
  const g = attachGates(page, [{ url: '/api/projects/p1/workbench', status: 409 }])
  await stubApi(page, {
    '/api/projects/p1/workbench': {
      __status: 409,
      __payload: { error: { code: 'conflict', message: '版本冲突', retryable: false } },
    },
  })
  await page.goto('/#/projects/p1/workbench')
  await expect(page.locator('[data-state="conflict"]')).toBeVisible()
  expectGatesClean(g)
})

test('console gate：已知 502 不能掩盖未知 API 子路径 404', async ({ page }) => {
  const g = attachGates(page, [{ url: '/api/overview', status: 502 }])
  await stubApi(page, {
    '/api/overview': {
      __status: 502,
      __payload: { error: { code: 'transport_lost', message: '连接中断', retryable: false } },
    },
  })
  await page.goto('/#/overview')
  await expect(page.locator('[data-state="degraded"]').first()).toBeVisible()
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
