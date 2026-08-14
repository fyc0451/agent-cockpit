import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * One ordinary live journey on one ephemeral server.
 * Selectors locked to Web exact eb75ace0f202174fdcb018a09c68c9b5219a5c05:
 *   新终端 / 中断 / 重连 / 重启 / 全屏 / 退出全屏 / 关闭标签页 / 关闭会话
 *   testids: terminal-tabs, terminal-tab-{id}, terminal-surface-{id}, terminal-runtime-state,
 *            terminal-fullscreen-overlay（即将到来的 Web rework contract）
 * Empty-registry is checked at 1280 and 390 before any write.
 * After writes, 390 is rechecked on that same populated state.
 * Missing Project / Workspace / TERM-003 controls fail. No blocked-return.
 */

const FORBIDDEN = /\b(cwd|command|pid|env|herdr_session|herdr_pane|HERDR_SESSION|HERDR_PANE_ID|HERDR_ENV)\b/

function attachLiveGates(page: Page) {
  const posts: { url: string; method: string; body: string }[] = []
  const forbidden: string[] = []
  page.on('request', (request: Request) => {
    const url = request.url()
    const body = request.postData() ?? ''
    if (request.method() === 'POST' || request.method() === 'PUT' || request.method() === 'PATCH') {
      posts.push({ url, method: request.method(), body })
    }
    if (FORBIDDEN.test(url) || FORBIDDEN.test(body)) {
      forbidden.push(`${request.method()} ${url} ${body.slice(0, 200)}`)
    }
  })
  return { posts, forbidden }
}

function terminalPosts(gates: { posts: { url: string }[] }, since: number) {
  return gates.posts.slice(since).filter((item) => item.url.includes('terminal-tickets'))
}

async function expectNoHorizontalOverflow(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

async function expectEmptyProjects(page: Page) {
  await expect(page.getByText('还没有项目')).toBeVisible()
  await expect(page.getByRole('button', { name: '选择项目目录' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
}

async function expectLiveStream(page: Page) {
  const state = page.getByTestId('terminal-runtime-state')
  await expect(state, 'TERM-003 live stream must be visible').toBeVisible({ timeout: 20_000 })
  await expect(state).toContainText('runtime=running')
  await expect(state).toContainText('流=live')
}

async function visibleSurface(page: Page) {
  const live = page.locator('[data-testid^="terminal-surface-"]:visible')
  await expect(live, 'live ticket surface terminal-surface-{ticketId} is required').toBeVisible()
  return live.first()
}

function fullscreenOverlay(page: Page) {
  return page.getByTestId('terminal-fullscreen-overlay')
}

async function enterFullscreen(page: Page) {
  const enter = page.getByRole('button', { name: '全屏' })
  await expect(enter, '全屏 control is required').toBeVisible()
  await expect(enter).toBeEnabled()
  await enter.click()
  await expect(fullscreenOverlay(page), 'fullscreen overlay testid is required').toBeVisible()
}

async function expectExitFullscreenClickable(page: Page) {
  const overlay = fullscreenOverlay(page)
  const exitBtn = overlay.getByRole('button', { name: '退出全屏' })
  await expect(exitBtn, 'overlay must contain a clickable 退出全屏').toBeVisible()
  await expect(exitBtn).toBeEnabled()
  const box = await exitBtn.boundingBox()
  const vp = page.viewportSize()
  expect(box, '退出全屏 must have a box').not.toBeNull()
  expect(vp, 'viewport must be set').not.toBeNull()
  if (!box || !vp) return
  expect(box.width, '退出全屏 must be large enough to click').toBeGreaterThan(0)
  expect(box.height, '退出全屏 must be large enough to click').toBeGreaterThan(0)
  expect(box.x, '退出全屏 must stay in viewport').toBeGreaterThanOrEqual(0)
  expect(box.y, '退出全屏 must stay in viewport').toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(vp.width + 1)
  expect(box.y + box.height).toBeLessThanOrEqual(vp.height + 1)
  const clip = await exitBtn.evaluate((el) => ({
    overflowX: getComputedStyle(el).overflowX,
    textWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    pointerEvents: getComputedStyle(el).pointerEvents,
  }))
  expect(clip.pointerEvents, '退出全屏 must receive clicks').not.toBe('none')
  expect(clip.textWidth, '退出全屏 label must not be clipped').toBeLessThanOrEqual(clip.clientWidth + 1)
  return exitBtn
}

async function expectMainContainerUnobstructed(page: Page) {
  await expectNoHorizontalOverflow(page)
  const overlay = fullscreenOverlay(page)
  const metrics = await overlay.evaluate((el) => ({
    sw: el.scrollWidth,
    cw: el.clientWidth,
    overflowX: getComputedStyle(el).overflowX,
  }))
  expect(metrics.sw, 'fullscreen overlay must not overflow horizontally').toBeLessThanOrEqual(metrics.cw + 1)
}

async function exitFullscreenViaButton(page: Page) {
  const exitBtn = await expectExitFullscreenClickable(page)
  await exitBtn!.click()
  await expect(fullscreenOverlay(page)).toHaveCount(0)
}

async function exitFullscreenViaEscape(page: Page) {
  await expect(fullscreenOverlay(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(fullscreenOverlay(page)).toHaveCount(0)
}

test('TERM-003 live journey: create, input, output, fullscreen, resize, reload/replay, interrupt, restart, close-view, close-session', async ({
  page,
}) => {
  const gates = attachLiveGates(page)

  await page.setViewportSize({ width: 1280, height: 800 })
  const documentResponse = await page.goto('/#/projects')
  expect(documentResponse, 'document must come from the live server').not.toBeNull()
  await expectEmptyProjects(page)

  await page.setViewportSize({ width: 390, height: 844 })
  await expectEmptyProjects(page)

  await page.setViewportSize({ width: 1280, height: 800 })
  await page.getByRole('button', { name: '选择项目目录' }).click()
  const projectDialog = page.getByRole('dialog', { name: '添加项目' })
  await expect(projectDialog).toBeVisible()
  await projectDialog.getByRole('button', { name: /Local/ }).click()
  const homeRoot = projectDialog.getByRole('button', { name: /^home$/i })
  await expect(homeRoot, 'ephemeral HOME root must be listed as home').toBeVisible()
  await homeRoot.click()
  const seed = projectDialog.getByRole('button', { name: /term003-live-seed/ }).filter({ hasNotText: '进入' })
  await expect(seed, 'select term003-live-seed, not 进入').toBeVisible()
  await seed.click()
  await projectDialog.getByRole('button', { name: '识别所选目录' }).click()
  const submit = projectDialog.getByRole('button', { name: '确认添加 Project' })
  await expect(submit, 'register submit must become enabled after probe').toBeEnabled()
  await submit.click()
  await expect(projectDialog.getByText('登记成功')).toBeVisible({ timeout: 15_000 })

  await page.goto('/#/projects')
  const projectLink = page.locator('a.list-link, a.list-title').first()
  await expect(projectLink, 'registered Project must appear in the live list').toBeVisible()
  await projectLink.click()
  const createWorkspace = page.getByRole('button', { name: '创建 Workspace' })
  await expect(createWorkspace).toBeVisible()
  await expect(createWorkspace, 'Workspace create must be enabled after a live RepoLocation exists').toBeEnabled()
  await createWorkspace.click()
  const workspaceDialog = page.getByRole('dialog', { name: '创建 Workspace' })
  await expect(workspaceDialog).toBeVisible()
  await workspaceDialog.getByLabel('Workspace 名称').fill('live-terminal')
  await workspaceDialog.getByRole('button', { name: '确认创建' }).click()
  await expect(page).toHaveURL(/\/workspaces\/[^/]+/)

  const terminalCard = page.locator('.card', { hasText: '终端' }).first()
  await expect(terminalCard, 'Workspace home must expose a Terminal card').toBeVisible()
  await expect(terminalCard, 'terminal.pty must enable the Terminal card').not.toHaveAttribute(
    'aria-disabled',
    'true',
  )
  await terminalCard.click()
  await expect(page).toHaveURL(/\/terminal/)
  await expect(page.getByText('PTY 未接通')).toHaveCount(0)

  const create = page.getByRole('button', { name: '新终端' }).first()
  await expect(create, 'stable name 新终端 is required').toBeVisible()
  await expect(create).toBeEnabled()
  const createPostsBefore = gates.posts.length
  await create.click()
  await expect(page.getByTestId('terminal-tabs')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('[data-testid^="terminal-tab-"]').first()).toBeVisible()
  const createPosts = terminalPosts(gates, createPostsBefore)
  expect(
    createPosts.some((item) => /\/terminal-tickets$/.test(new URL(item.url).pathname)),
    'create must POST /terminal-tickets',
  ).toBe(true)

  await expect(
    page.getByText('正在连接终端流…').or(page.getByText('正在回放终端历史…')).or(page.getByTestId('terminal-runtime-state')),
  ).toBeVisible({ timeout: 20_000 })
  await expectLiveStream(page)
  const surface = await visibleSurface(page)

  const helper = page.locator('.xterm-helper-textarea')
  if ((await helper.count()) > 0) {
    await helper.focus()
  } else {
    await surface.click()
  }
  await page.keyboard.type('printf TERM003-LIVE')
  await page.keyboard.press('Enter')
  await expect
    .poll(
      async () => {
        const chunks = await page.locator('.xterm, .xterm-rows, [data-testid^="terminal-surface-"]').allInnerTexts()
        return chunks.join('\n')
      },
      { timeout: 15_000, message: 'PTY must show TERM003-LIVE (canvas-only output is a remaining Web gap)' },
    )
    .toContain('TERM003-LIVE')
  await expectLiveStream(page)

  await enterFullscreen(page)
  await expectMainContainerUnobstructed(page)
  await exitFullscreenViaButton(page)
  await enterFullscreen(page)
  await exitFullscreenViaEscape(page)

  const beforeBox = await surface.boundingBox()
  await page.setViewportSize({ width: 390, height: 844 })
  await expectNoHorizontalOverflow(page)
  await enterFullscreen(page)
  await expectMainContainerUnobstructed(page)
  await expectExitFullscreenClickable(page)
  await exitFullscreenViaButton(page)
  const phoneSurface = await visibleSurface(page)
  const phoneBox = await phoneSurface.boundingBox()
  expect(phoneBox, 'resize must keep a visible terminal surface').not.toBeNull()
  if (beforeBox && phoneBox) {
    expect(phoneBox.width, '390px surface width must change with viewport').not.toBe(beforeBox.width)
  }
  await expectLiveStream(page)

  await page.reload()
  await expect(page).toHaveURL(/\/terminal/)
  await expect(
    page.getByText('正在回放终端历史…').or(page.getByTestId('terminal-runtime-state')),
  ).toBeVisible({ timeout: 20_000 })
  const disconnected = page.getByText('终端流已断开')
  if ((await disconnected.count()) > 0) {
    const reconnect = page.getByRole('button', { name: '重连' })
    await expect(reconnect, '重连 must be enabled after stream disconnect').toBeEnabled()
    await reconnect.click()
  }
  await expectLiveStream(page)

  const interrupt = page.getByRole('button', { name: '中断' })
  await expect(interrupt).toBeVisible()
  await expect(interrupt, '中断 is enabled only in live').toBeEnabled()
  const interruptBefore = gates.posts.length
  await interrupt.click()
  await expect
    .poll(() => terminalPosts(gates, interruptBefore).some((item) => item.url.endsWith('/interrupt')))
    .toBe(true)

  const restart = page.getByRole('button', { name: '重启' })
  await expect(restart).toBeVisible()
  await expect(restart).toBeEnabled()
  const restartBefore = gates.posts.length
  await restart.click()
  await expect
    .poll(() => terminalPosts(gates, restartBefore).some((item) => item.url.endsWith('/restart')))
    .toBe(true)
  await expectLiveStream(page)

  const closeViewBefore = gates.posts.length
  await page.getByRole('button', { name: '关闭标签页' }).click()
  const closeViewPosts = terminalPosts(gates, closeViewBefore)
  expect(
    closeViewPosts.filter((item) => item.url.endsWith('/close')),
    '关闭标签页 must not POST /close',
  ).toEqual([])
  await expect(page.locator('[data-testid^="terminal-tab-"]').first()).toHaveClass(/terminal-tab--detached/)

  await page.locator('[data-testid^="terminal-tab-"] button.terminal-tab-label').first().click()
  await expectLiveStream(page)

  const headerClose = page.getByRole('button', { name: '关闭会话' }).first()
  await expect(headerClose).toBeVisible()
  await expect(headerClose).toBeEnabled()
  const closeBefore = gates.posts.length
  await headerClose.click()
  await expect(page.getByText('确认关闭会话？')).toBeVisible()
  expect(
    terminalPosts(gates, closeBefore).filter((item) => item.url.endsWith('/close')),
    'first 关闭会话 click is confirm-only',
  ).toEqual([])
  await page.getByRole('button', { name: '关闭会话' }).last().click()
  await expect
    .poll(() => terminalPosts(gates, closeBefore).filter((item) => item.url.endsWith('/close')).length)
    .toBe(1)
  await expect(page.getByText('终端会话已停止')).toBeVisible()

  await page.setViewportSize({ width: 1280, height: 800 })
  await expectNoHorizontalOverflow(page)
  expect(gates.forbidden, 'browser must not send cwd/command/PID/env/Herdr').toEqual([])
})
