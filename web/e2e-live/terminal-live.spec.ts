import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * Ordinary Project → Workspace → Terminal live journey.
 * Talks only to PLAYWRIGHT_LIVE_BASE_URL (real ephemeral Next). No API stubs.
 * Browser must never submit cwd / command / PID / env / Herdr identifiers.
 */

const FORBIDDEN = /\b(cwd|command|pid|env|herdr_session|herdr_pane|HERDR_SESSION|HERDR_PANE_ID|HERDR_ENV)\b/

function attachLiveGates(page: Page) {
  const posts: { url: string; body: string }[] = []
  const forbidden: string[] = []
  let wsCount = 0
  page.on('websocket', () => {
    wsCount += 1
  })
  page.on('request', (request: Request) => {
    const url = request.url()
    const body = request.postData() ?? ''
    if (request.method() === 'POST' || request.method() === 'PUT' || request.method() === 'PATCH') {
      posts.push({ url, body })
    }
    if (FORBIDDEN.test(url) || FORBIDDEN.test(body)) {
      forbidden.push(`${request.method()} ${url} ${body.slice(0, 200)}`)
    }
  })
  return {
    posts,
    forbidden,
    ws: () => wsCount,
  }
}

async function expectNoHorizontalOverflow(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

test.describe.configure({ mode: 'serial' })

test('empty Registry is live and empty', async ({ page }) => {
  const gates = attachLiveGates(page)
  const response = await page.goto('/#/projects')
  expect(response, 'document must come from the live server').not.toBeNull()
  await expect(page.getByText('还没有项目')).toBeVisible()
  await expect(page.getByRole('button', { name: '选择项目目录' })).toBeVisible()
  expect(gates.forbidden, 'browser must not send cwd/command/PID/env/Herdr').toEqual([])
  await expectNoHorizontalOverflow(page)
})

test('Project wizard opens against live runtime-nodes', async ({ page }) => {
  const gates = attachLiveGates(page)
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '选择项目目录' }).click()
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: /Local|本机/i })).toBeVisible()
  expect(gates.forbidden).toEqual([])
})

test('register seeded directory when discovery roots exist', async ({ page }) => {
  const gates = attachLiveGates(page)
  await page.goto('/#/projects')
  await page.getByRole('button', { name: '选择项目目录' }).click()
  const dialog = page.getByRole('dialog', { name: '添加项目' })
  await dialog.getByRole('button', { name: /Local|本机/i }).click()
  const rootButtons = dialog.getByRole('button').filter({ hasNotText: /取消|关闭|上一步/ })
  await expect(rootButtons.first()).toBeVisible()
  await rootButtons.first().click()
  const seed = dialog.getByRole('button', { name: /term003-live-seed/ })
  if ((await seed.count()) === 0) {
    test.info().annotations.push({
      type: 'blocked',
      description:
        'live discovery did not list term003-live-seed; Registry/root allowlist is a Web or discovery-root dependency',
    })
    expect(gates.forbidden).toEqual([])
    return
  }
  await seed.click()
  const probe = dialog.getByRole('button', { name: /识别/ })
  await probe.click()
  const submit = dialog.getByRole('button', { name: '确认添加 Project' })
  if (await submit.isEnabled()) {
    await submit.click()
    await expect(dialog.getByText(/登记成功|已添加/)).toBeVisible({ timeout: 15_000 })
  } else {
    test.info().annotations.push({
      type: 'blocked',
      description: 'register submit stayed disabled after probe; Web writer / discovery fingerprint wiring',
    })
  }
  expect(gates.forbidden).toEqual([])
})

test('create Workspace from registered Project when the gate is open', async ({ page }) => {
  const gates = attachLiveGates(page)
  await page.goto('/#/projects')
  const projectLink = page.locator('a.list-link, a.list-title').first()
  if ((await page.getByText('还没有项目').count()) > 0 || (await projectLink.count()) === 0) {
    test.info().annotations.push({
      type: 'blocked',
      description: 'no registered Project yet; Workspace create waits on live registration',
    })
    expect(gates.forbidden).toEqual([])
    return
  }
  await projectLink.click()
  const create = page.getByRole('button', { name: '创建 Workspace' })
  await expect(create).toBeVisible()
  if ((await create.getAttribute('aria-disabled')) === 'true' || (await create.isDisabled())) {
    test.info().annotations.push({
      type: 'blocked',
      description: '创建 Workspace disabled: RepoLocation gate or Web writer workbench wiring',
    })
    expect(gates.forbidden).toEqual([])
    return
  }
  await create.click()
  const dialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('Workspace 名称').fill('live-terminal')
  await dialog.getByRole('button', { name: '确认创建' }).click()
  await expect(page).toHaveURL(/\/workspaces\/[^/]+/)
  expect(gates.forbidden).toEqual([])
})

test('Terminal page is reachable and control UI stays fail-closed until Web writer', async ({ page }) => {
  const gates = attachLiveGates(page)
  await page.goto('/#/projects')
  const workspaceLink = page.locator('a[href*="/workspaces/"]').first()
  if ((await workspaceLink.count()) > 0) {
    await workspaceLink.click()
    const terminalCard = page.locator('.card', { hasText: '终端' }).first()
    if ((await terminalCard.getAttribute('aria-disabled')) === 'true') {
      await expect(terminalCard).toContainText(/PTY|未接通|deferred|terminal/i)
      test.info().annotations.push({
        type: 'blocked',
        description:
          'terminal.pty or terminal.control.ui is still W1: no start/input/resize/reconnect/kill via UI',
      })
    } else {
      await terminalCard.click()
    }
  } else {
    await page.goto('/#/projects/missing/workspaces/missing/terminal')
    await expect(page.getByText(/项目不存在|PTY 未接通|终端/)).toBeVisible()
  }

  const surface = page.getByTestId('terminal-surface')
  if (await surface.count()) {
    await expect(surface).toBeVisible()
  }
  const interrupt = page.getByRole('button', { name: '中断' })
  if (await interrupt.count()) {
    await expect(interrupt).toBeDisabled()
    await expect(page.getByRole('button', { name: '重连' })).toBeDisabled()
    await expect(page.getByRole('button', { name: '重启' })).toBeDisabled()
  }
  expect(gates.ws(), 'W1 must not open a PTY websocket').toBe(0)
  const terminalPosts = gates.posts.filter((item) => /terminal/i.test(item.url))
  expect(terminalPosts, 'no terminal control POST until Web writer wires the UI').toEqual([])
  expect(gates.forbidden).toEqual([])
  await expectNoHorizontalOverflow(page)
})

test('390px and desktop keep the live shell usable', async ({ page }) => {
  const gates = attachLiveGates(page)
  await page.goto('/#/projects')
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.goto('/#/projects/missing/workspaces/missing/terminal')
  await expect(page.locator('.page-title, [class*="state"]').first()).toBeVisible()
  await expectNoHorizontalOverflow(page)
  expect(gates.forbidden).toEqual([])
})
