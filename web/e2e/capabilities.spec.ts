import { expect, test } from '@playwright/test'
import { metaOk, projectP1 } from '../fixtures/api'
import { apiCalls, attachGates, expectGatesClean, stubApi } from './helpers'

test('files.read 关闭：forbidden 可见，/api/files 请求=0，WS=0', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/files')
  await expect(page.locator('[data-state="forbidden"]')).toBeVisible()
  await expect(page.getByText(/Workspace 文件 facade API 未接通/)).toBeVisible()
  // 等 project 等其它请求落地
  await expect(page.locator('.topbar')).toContainText('Project One')
  expect(apiCalls(g, '/api/files')).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})

test('terminal cap=false：disconnected banner + 按钮 disabled，POST=0，WS=0', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/terminal')
  await expect(page.locator('[data-state="disconnected"]')).toBeVisible()
  for (const name of ['中断', '重连', '重启']) {
    await expect(page.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
  }
  await expect(page.locator('.topbar')).toContainText('Project One')
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})

test('Project cap terminal.pty=true 不得开启 Workspace PTY，POST=0，WS=0', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/projects/p1': {
      data: projectP1,
      meta: { ...metaOk, capabilities: { 'terminal.pty': { available: true, reason: null } } },
    },
  })
  await page.goto('/#/projects/p1/workspaces/w1/terminal')
  await expect(page.locator('[data-state="disconnected"]')).toBeVisible()
  await expect(page.getByText(/已由服务端 capability 标记为可用/)).toHaveCount(0)
  for (const name of ['中断', '重连', '重启']) {
    await expect(page.getByRole('button', { name })).toHaveAttribute('aria-disabled', 'true')
  }
  expect(g.postRequests).toEqual([])
  expect(g.wsCount).toBe(0)
  expectGatesClean(g)
})

test('Project cap files.read=true 不得开启同 Project 任一 Workspace 文件能力', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/projects/p1': {
      data: projectP1,
      meta: { ...metaOk, capabilities: { 'files.read': { available: true, reason: null } } },
    },
  })
  for (const workspaceId of ['w1', 'w2']) {
    await page.goto(`/#/projects/p1/workspaces/${workspaceId}/files`)
    await expect(page.locator('[data-state="forbidden"]')).toBeVisible()
    await expect(page.locator('[data-state="empty"]')).toHaveCount(0)
  }
  expect(apiCalls(g, '/api/files')).toEqual([])
  expectGatesClean(g)
})

test('Remote workspace disabled：点击 0 请求且不跳转', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1')
  await expect(page.locator('.page-title')).toHaveText('本机工作区')

  await page.getByTitle('切换 Workspace').click()
  const dialog = page.getByRole('dialog', { name: 'Workspace 切换' })
  const remoteBtn = dialog.getByRole('button', { name: /远程 GPU/ })
  await expect(remoteBtn).toHaveAttribute('aria-disabled', 'true')
  // 原因可读（aria-describedby）
  const descId = await remoteBtn.getAttribute('aria-describedby')
  expect(descId).toBeTruthy()
  await expect(dialog.locator(`[id="${descId}"]`)).toContainText('远程 Herdr 控制未接通')

  const hashBefore = page.url()
  const callsBefore = g.apiRequests.length
  // Playwright 把 aria-disabled 视为不可点击（actionability），force 绕过；
  // 事件仍会触发，用来验证应用的拦截逻辑（0 请求、不跳转、dialog 不关）
  await remoteBtn.click({ force: true })
  expect(page.url()).toBe(hashBefore)
  expect(g.apiRequests.length).toBe(callsBefore)
  await expect(dialog).toBeVisible()
  expectGatesClean(g)
})
