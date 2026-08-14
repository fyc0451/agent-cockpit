import { writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * One ordinary live journey on one ephemeral server.
 * Selectors locked to Web exact 173341dad1d8022aa42ae73f7463fe9ad706b209:
 *   新终端 / 中断 / 重连 / 重启 / 全屏 (exact) / 退出全屏 (exact) / 关闭标签页 / 关闭会话
 *   testids: terminal-tabs, terminal-tab-{id}, terminal-surface-{id}, terminal-runtime-state
 *   overlay: .terminal-fullscreen (173341d; not data-testid=terminal-fullscreen-overlay)
 * Empty-registry is checked at 1280 and 390 before any write.
 * After writes, 390 is rechecked on that same populated state.
 * Missing Project / Workspace / TERM-003 controls fail. No blocked-return.
 */

const WEB_EXACT = '173341dad1d8022aa42ae73f7463fe9ad706b209'
const FORBIDDEN = /\b(cwd|command|pid|env|herdr_session|herdr_pane|HERDR_SESSION|HERDR_PANE_ID|HERDR_ENV)\b/
const OUTPUT_LIVE = 'out.live.7a3c91'
const OUTPUT_390 = 'out.w390.b82e04'
const OUTPUT_INT = 'out.int.d19f6a'
const OUTPUT_RST = 'out.rst.e40c28'

type GatePost = { url: string; method: string; body: string }
type GateResponse = { url: string; status: number; ok: boolean }
type GateFailure = { url: string; method: string; error: string }
type LiveGates = {
  posts: GatePost[]
  responses: GateResponse[]
  failed: GateFailure[]
  forbidden: string[]
  wsSent: string[]
}

function attachLiveGates(page: Page): LiveGates {
  const gates: LiveGates = { posts: [], responses: [], failed: [], forbidden: [], wsSent: [] }
  page.on('request', (request: Request) => {
    const url = request.url()
    const body = request.postData() ?? ''
    if (request.method() === 'POST' || request.method() === 'PUT' || request.method() === 'PATCH') {
      gates.posts.push({ url, method: request.method(), body })
    }
    if (FORBIDDEN.test(url) || FORBIDDEN.test(body)) {
      gates.forbidden.push(`${request.method()} ${url} ${body.slice(0, 200)}`)
    }
  })
  page.on('response', (response) => {
    if (!response.url().includes('terminal-tickets')) return
    gates.responses.push({
      url: response.url(),
      status: response.status(),
      ok: response.ok(),
    })
  })
  page.on('requestfailed', (request) => {
    gates.failed.push({
      url: request.url(),
      method: request.method(),
      error: request.failure()?.errorText ?? 'requestfailed',
    })
  })
  page.on('websocket', (ws) => {
    if (!ws.url().includes('terminal-tickets')) return
    ws.on('framesent', (event) => {
      if (typeof event.payload === 'string') gates.wsSent.push(event.payload)
    })
  })
  return gates
}

function persistDiagnostics(gates: LiveGates) {
  const dir = process.env.PLAYWRIGHT_LIVE_ARTIFACT_DIR
  if (!dir) return
  writeFileSync(
    join(dir, 'e2e-diagnostics.json'),
    `${JSON.stringify(
      {
        web_exact: WEB_EXACT,
        posts: gates.posts,
        responses: gates.responses,
        failed: gates.failed,
        forbidden: gates.forbidden,
        ws_sent: gates.wsSent,
      },
      null,
      2,
    )}\n`,
    'utf8',
  )
}

function terminalPosts(gates: LiveGates, since: number) {
  return gates.posts.slice(since).filter((item) => item.url.includes('terminal-tickets'))
}

function isCreateTicketUrl(url: string) {
  try {
    return /\/terminal-tickets$/.test(new URL(url).pathname)
  } catch {
    return false
  }
}

function createTicketPosts(gates: LiveGates, since = 0) {
  return terminalPosts(gates, since).filter((item) => isCreateTicketUrl(item.url))
}

function terminalControlResponses(gates: LiveGates, since: number, suffix: string) {
  return gates.responses.slice(since).filter((item) => {
    try {
      return new URL(item.url).pathname.endsWith(suffix)
    } catch {
      return item.url.endsWith(suffix)
    }
  })
}

function parseJsonObject(raw: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(raw) as unknown
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
    return value as Record<string, unknown>
  } catch {
    return null
  }
}

function lastKnownDims(gates: LiveGates): { cols: number; rows: number } | null {
  for (let index = gates.wsSent.length - 1; index >= 0; index -= 1) {
    const frame = parseJsonObject(gates.wsSent[index] ?? '')
    if (
      frame?.type === 'resize' &&
      typeof frame.cols === 'number' &&
      typeof frame.rows === 'number' &&
      frame.cols > 0 &&
      frame.rows > 0
    ) {
      return { cols: frame.cols, rows: frame.rows }
    }
  }
  for (let index = gates.posts.length - 1; index >= 0; index -= 1) {
    const post = gates.posts[index]
    if (!post || !isCreateTicketUrl(post.url)) continue
    const body = parseJsonObject(post.body)
    if (
      body &&
      typeof body.cols === 'number' &&
      typeof body.rows === 'number' &&
      body.cols > 0 &&
      body.rows > 0
    ) {
      return { cols: body.cols, rows: body.rows }
    }
  }
  return null
}

function expectTerminalHealth(gates: LiveGates) {
  const terminalFailed = gates.failed.filter(
    (item) => item.url.includes('terminal-tickets') || item.url.includes('/api/'),
  )
  expect(terminalFailed, 'terminal/API requestfailed must fail-closed').toEqual([])
  const bad = gates.responses.filter((item) => !item.ok || item.status < 200 || item.status >= 300)
  expect(bad, 'non-success terminal-tickets responses must fail-closed').toEqual([])
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

async function readAuthorityFence(page: Page) {
  const text = await page.getByTestId('terminal-runtime-state').innerText()
  const generation = /generation=(\d+)/.exec(text)
  const revision = /revision=(\d+)/.exec(text)
  expect(generation, `authority generation missing in ${text}`).not.toBeNull()
  expect(revision, `authority revision missing in ${text}`).not.toBeNull()
  return {
    generation: Number(generation![1]),
    revision: Number(revision![1]),
    text,
  }
}

async function visibleSurface(page: Page) {
  const live = page.locator('[data-testid^="terminal-surface-"]:visible')
  await expect(live, 'live ticket surface terminal-surface-{ticketId} is required').toBeVisible()
  return live.first()
}

async function readTicketId(page: Page) {
  const surface = await visibleSurface(page)
  const testid = await surface.getAttribute('data-testid')
  expect(testid, 'surface testid must encode ticket id').toMatch(/^terminal-surface-/)
  return testid!.slice('terminal-surface-'.length)
}

function fullscreenOverlay(page: Page) {
  return page.locator('.terminal-fullscreen')
}

async function enterFullscreen(page: Page) {
  await expect(fullscreenOverlay(page), 'overlay must be absent before enter').toHaveCount(0)
  const enter = page.getByRole('button', { name: '全屏', exact: true })
  await expect(enter, '全屏 control is required').toBeVisible()
  await expect(enter).toBeEnabled()
  await enter.click()
  await expect(fullscreenOverlay(page), '173341d .terminal-fullscreen overlay is required').toBeVisible()
}

async function expectExitFullscreenClickable(page: Page) {
  const overlay = fullscreenOverlay(page)
  const exitBtn = overlay.getByRole('button', { name: '退出全屏', exact: true })
  await expect(exitBtn, 'overlay must contain a clickable 退出全屏').toBeVisible()
  await expect(exitBtn).toBeEnabled()
  const box = await exitBtn.boundingBox()
  const vp = page.viewportSize()
  expect(box, '退出全屏 must have a box').not.toBeNull()
  expect(vp, 'viewport must be set').not.toBeNull()
  if (box === null || vp === null) {
    throw new Error('exit fullscreen box/viewport missing')
  }
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
  expect(clip.overflowX, '退出全屏 must not scroll-clip its label').not.toBe('scroll')
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
  expect(metrics.overflowX, 'fullscreen overlay must not scroll horizontally').not.toBe('scroll')
  expect(metrics.sw, 'fullscreen overlay must not overflow horizontally').toBeLessThanOrEqual(metrics.cw + 1)
}

async function exitFullscreenViaButton(page: Page) {
  const exitBtn = await expectExitFullscreenClickable(page)
  await exitBtn.click()
  await expect(fullscreenOverlay(page)).toHaveCount(0)
}

async function exitFullscreenViaEscape(page: Page) {
  await expect(fullscreenOverlay(page)).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(fullscreenOverlay(page)).toHaveCount(0)
}

async function expectMarker(page: Page, marker: string) {
  await expect
    .poll(
      async () => {
        const chunks = await page.locator('.xterm, .xterm-rows, [data-testid^="terminal-surface-"]').allInnerTexts()
        return chunks.join('\n')
      },
      { timeout: 15_000, message: `PTY must show ${marker} (canvas-only output is a remaining Web gap)` },
    )
    .toContain(marker)
}

function decodePrintfCommand(output: string): string {
  const encoded = Buffer.from(output, 'utf8').toString('base64')
  const command = `printf '%s\\n' "$(printf %s ${encoded} | base64 -d)"`
  if (command.includes(output)) {
    throw new Error('output nonce leaked into typed command')
  }
  return command
}

async function typeCommand(page: Page, command: string, marker: string) {
  const surface = await visibleSurface(page)
  const helper = page.locator('.xterm-helper-textarea')
  if ((await helper.count()) > 0) {
    await helper.focus()
  } else {
    await surface.click()
  }
  await page.keyboard.type(command)
  await page.keyboard.press('Enter')
  await expectMarker(page, marker)
  await expectLiveStream(page)
}

async function typeDecodedOutput(page: Page, output: string) {
  await typeCommand(page, decodePrintfCommand(output), output)
}

async function expectSuccessfulControl(gates: LiveGates, since: number, suffix: string) {
  await expect
    .poll(() => {
      const hits = terminalControlResponses(gates, since, suffix)
      return hits.length > 0 && hits.every((item) => item.ok && item.status >= 200 && item.status < 300)
    }, { timeout: 15_000, message: `${suffix} must return a successful terminal response` })
    .toBe(true)
}

test('TERM-003 live journey: create, input, output, fullscreen, resize, reload/replay, interrupt, restart, close-view, close-session', async ({
  page,
}) => {
  const gates = attachLiveGates(page)
  try {
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
    const uploadsRoot = projectDialog.getByRole('button', { name: /^uploads$/i })
    await expect(uploadsRoot, 'runner-owned uploads root must be listed').toBeVisible()
    await uploadsRoot.click()
    const seed = projectDialog.getByRole('button', { name: /term003-live-seed/ }).filter({ hasNotText: '进入' })
    await expect(seed, 'select term003-live-seed, not 进入').toBeVisible()
    await seed.click()
    await projectDialog.getByRole('button', { name: '检查并继续' }).click()
    const submit = projectDialog.getByRole('button', { name: '确认添加' })
    await expect(submit, 'register submit must become enabled after probe').toBeEnabled()
    await submit.click()
    await expect(projectDialog.getByText('登记成功')).toBeVisible({ timeout: 15_000 })
    const backToList = projectDialog.getByRole('button', { name: '返回列表' })
    await expect(backToList, '登记成功 must offer 返回列表').toBeVisible()
    await backToList.click()
    await expect(projectDialog, 'add-project dialog must close before list click').toHaveCount(0)

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
    await expect(page).toHaveURL(/\/workspaces\/[^/]+\/files$/)
    const workspaceHome = page.getByRole('link', { name: 'live-terminal', exact: true })
    await expect(workspaceHome, 'sidebar must expose exact live-terminal home link').toBeVisible()
    await workspaceHome.click()
    await expect(page).toHaveURL(/\/workspaces\/[^/]+$/)

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
    const createPosts = createTicketPosts(gates, createPostsBefore)
    expect(createPosts.length, 'create must POST /terminal-tickets exactly once').toBe(1)

    await expect(
      page.getByText('正在连接终端流…').or(page.getByText('正在回放终端历史…')).or(page.getByTestId('terminal-runtime-state')),
    ).toBeVisible({ timeout: 20_000 })
    await expectLiveStream(page)
    const ticketId = await readTicketId(page)
    await typeDecodedOutput(page, OUTPUT_LIVE)

    await enterFullscreen(page)
    await expectMainContainerUnobstructed(page)
    await exitFullscreenViaButton(page)
    await enterFullscreen(page)
    await exitFullscreenViaEscape(page)

    const desktopDims = lastKnownDims(gates)
    expect(desktopDims, 'desktop must expose create/resize cols/rows').not.toBeNull()
    const beforeBox = await (await visibleSurface(page)).boundingBox()
    expect(beforeBox, 'desktop surface must have a box before 390').not.toBeNull()
    const resizeBefore = gates.wsSent.length
    await page.setViewportSize({ width: 390, height: 844 })
    await expectNoHorizontalOverflow(page)
    await expect
      .poll(
        () => {
          const frames = gates.wsSent.slice(resizeBefore).flatMap((raw) => {
            const frame = parseJsonObject(raw)
            return frame?.type === 'resize' ? [frame] : []
          })
          return frames.some(
            (frame) =>
              typeof frame.cols === 'number' &&
              typeof frame.rows === 'number' &&
              (frame.cols !== desktopDims!.cols || frame.rows !== desktopDims!.rows),
          )
        },
        { timeout: 10_000, message: '390 must send a resize frame with changed authority dims' },
      )
      .toBe(true)
    const phoneSurface = await visibleSurface(page)
    const phoneBox = await phoneSurface.boundingBox()
    expect(phoneBox, 'resize must keep a visible terminal surface').not.toBeNull()
    expect(phoneBox!.width, '390px surface width must change with viewport').not.toBe(beforeBox!.width)
    await typeDecodedOutput(page, OUTPUT_390)

    await enterFullscreen(page)
    await expectMainContainerUnobstructed(page)
    await expectExitFullscreenClickable(page)
    await exitFullscreenViaButton(page)
    await expectLiveStream(page)

    const createsBeforeReload = createTicketPosts(gates).length
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
    expect(await readTicketId(page), 'reload must reopen the same ticket').toBe(ticketId)
    expect(createTicketPosts(gates).length, 'reload must not POST a replacement create').toBe(createsBeforeReload)
    await expectMarker(page, OUTPUT_LIVE)

    const beforeInterrupt = await readAuthorityFence(page)
    const interrupt = page.getByRole('button', { name: '中断' })
    await expect(interrupt).toBeVisible()
    await expect(interrupt, '中断 is enabled only in live').toBeEnabled()
    const interruptSince = gates.responses.length
    await interrupt.click()
    await expectSuccessfulControl(gates, interruptSince, '/interrupt')
    await expectLiveStream(page)
    const afterInterrupt = await readAuthorityFence(page)
    expect(
      afterInterrupt.generation !== beforeInterrupt.generation ||
        afterInterrupt.revision !== beforeInterrupt.revision,
      `interrupt must change authority fence (${beforeInterrupt.text} -> ${afterInterrupt.text})`,
    ).toBe(true)
    await typeDecodedOutput(page, OUTPUT_INT)

    const beforeRestart = await readAuthorityFence(page)
    const restart = page.getByRole('button', { name: '重启' })
    await expect(restart).toBeVisible()
    await expect(restart).toBeEnabled()
    const restartSince = gates.responses.length
    await restart.click()
    await expectSuccessfulControl(gates, restartSince, '/restart')
    await expectLiveStream(page)
    const afterRestart = await readAuthorityFence(page)
    expect(afterRestart.generation, 'restart must advance engine generation').toBeGreaterThan(beforeRestart.generation)
    expect(
      afterRestart.generation !== beforeRestart.generation || afterRestart.revision !== beforeRestart.revision,
      `restart must change authority fence (${beforeRestart.text} -> ${afterRestart.text})`,
    ).toBe(true)
    await typeDecodedOutput(page, OUTPUT_RST)

    const closeViewBefore = gates.posts.length
    await page.getByRole('button', { name: '关闭标签页' }).click()
    await expect(page.locator('[data-testid^="terminal-tab-"]').first()).toHaveClass(/terminal-tab--detached/)
    expect(terminalPosts(gates, closeViewBefore), '关闭标签页 must issue zero terminal POSTs').toEqual([])

    await page.locator('[data-testid^="terminal-tab-"] button.terminal-tab-label').first().click()
    await expectLiveStream(page)
    expect(terminalPosts(gates, closeViewBefore), 'close-view window must stay at zero terminal POSTs').toEqual([])

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
    expectTerminalHealth(gates)
  } finally {
    persistDiagnostics(gates)
  }
})
