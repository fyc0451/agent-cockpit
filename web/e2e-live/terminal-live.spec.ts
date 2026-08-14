import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * One ordinary live journey on one ephemeral server.
 * Empty-registry checks happen before any write, at both 1280 and 390.
 * After writes, both viewports are rechecked on that same populated state.
 * Missing Project / Workspace / TERM-003 controls fail the run. No blocked-return.
 * Browser must never submit cwd / command / PID / env / Herdr identifiers.
 */

const FORBIDDEN = /\b(cwd|command|pid|env|herdr_session|herdr_pane|HERDR_SESSION|HERDR_PANE_ID|HERDR_ENV)\b/

function attachLiveGates(page: Page) {
  const posts: { url: string; body: string }[] = []
  const forbidden: string[] = []
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
  return { posts, forbidden }
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

test('TERM-003 live journey: empty, register, workspace, terminal, both viewports', async ({
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
  await projectDialog.getByRole('button', { name: /Local|本机/i }).click()
  const rootButtons = projectDialog.getByRole('button').filter({ hasNotText: /取消|关闭|上一步/ })
  await expect(rootButtons.first(), 'live discovery must expose at least one root').toBeVisible()
  await rootButtons.first().click()
  const seed = projectDialog.getByRole('button', { name: /term003-live-seed/ })
  await expect(seed, 'seeded directory must be listed; missing seed is a failed journey step').toBeVisible()
  await seed.click()
  await projectDialog.getByRole('button', { name: /识别/ }).click()
  const submit = projectDialog.getByRole('button', { name: '确认添加 Project' })
  await expect(submit, 'register submit must become enabled after probe').toBeEnabled()
  await submit.click()
  await expect(projectDialog.getByText(/登记成功|已添加/)).toBeVisible({ timeout: 15_000 })

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
  await expect(terminalCard, 'Terminal card must be enabled for TERM-003').not.toHaveAttribute(
    'aria-disabled',
    'true',
  )
  await terminalCard.click()
  await expect(page).toHaveURL(/\/terminal/)
  await expect(page.getByTestId('terminal-surface')).toBeVisible()

  const start = page.getByRole('button', { name: /启动终端|新建终端|创建终端|打开终端/ })
  await expect(
    start,
    'TERM-003 start/create control is required; W1 disabled shell is not acceptance',
  ).toBeVisible()
  await expect(start).toBeEnabled()
  await start.click()

  const surface = page.getByTestId('terminal-surface')
  await surface.click()
  await page.keyboard.type('printf TERM003-LIVE\\n')
  await page.keyboard.press('Enter')
  await expect(surface, 'PTY must echo the stable command output').toContainText('TERM003-LIVE', {
    timeout: 15_000,
  })

  const beforeBox = await surface.boundingBox()
  await page.setViewportSize({ width: 390, height: 844 })
  await expectNoHorizontalOverflow(page)
  await expect(surface).toBeVisible()
  const phoneBox = await surface.boundingBox()
  expect(phoneBox, 'resize must keep a visible terminal surface').not.toBeNull()
  if (beforeBox && phoneBox) {
    expect(phoneBox.width, '390px surface width must change with viewport').not.toBe(beforeBox.width)
  }

  await page.reload()
  await expect(page).toHaveURL(/\/terminal/)
  await expect(surface).toBeVisible()
  const reconnect = page.getByRole('button', { name: '重连' })
  await expect(reconnect, 'TERM-003 reconnect is required after reload').toBeVisible()
  if (await reconnect.isEnabled()) {
    await reconnect.click()
  }
  await expect(surface).toBeVisible()

  const interrupt = page.getByRole('button', { name: '中断' })
  const restart = page.getByRole('button', { name: '重启' })
  await expect(interrupt, 'TERM-003 interrupt is required').toBeVisible()
  await expect(restart, 'TERM-003 restart is required').toBeVisible()
  await expect(interrupt).toBeEnabled()
  await interrupt.click()
  await expect(restart).toBeEnabled()
  await restart.click()

  const closeViewPostsBefore = gates.posts.length
  await page.goto('/#/projects')
  const closeViewPosts = gates.posts.slice(closeViewPostsBefore)
  expect(
    closeViewPosts.filter((item) => /terminal/i.test(item.url)),
    'leaving the terminal view must not POST',
  ).toEqual([])

  await page.goBack()
  const close = page.getByRole('button', { name: '关闭' })
  await expect(close, 'TERM-003 explicit close is required').toBeVisible()
  await expect(close).toBeEnabled()
  await close.click()

  await page.setViewportSize({ width: 1280, height: 800 })
  await expectNoHorizontalOverflow(page)
  expect(gates.forbidden, 'browser must not send cwd/command/PID/env/Herdr').toEqual([])
})
