import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * Checkpoint A Local Focus 真实浏览器旅程（Wiki 37 §1/§4/§6）。
 *
 * 运行方式（本仓库不新增 launcher，复用既有 ephemeral harness）：
 *   1. runtime_root=$(mktemp -d) && chmod 700 "$runtime_root"
 *   2. scripts/next_ephemeral_server.py --runtime-root "$runtime_root" --source-sha <HEAD>
 *      （stdout 首行为 base_url；随机非保留 loopback 端口，禁 8790/18790）
 *   3. 用 agent_cockpit.project_registry_store 直接在 runtime_root/data 建立
 *      active Project/RepoLocation/Workspace，把 {slug, workspace_id} 写入 seed JSON。
 *   4. npm --prefix web run build 后：
 *      PLAYWRIGHT_LIVE_BASE_URL=<base_url> PLAYWRIGHT_LIVE_FOCUS_SEED=<seed.json> \
 *      npx playwright test -c playwright.live.config.ts e2e-live/checkpoint-a-focus.spec.ts
 *
 * 覆盖：保存 -> 刷新/离开返回同原文；零 Agent API/不读 transcript；/agent 回 Focus；
 * 1440/1366/1280/390 无横向溢出；浅/深主题；键盘可达；Files/Terminal 深链不回归。
 */

const SEED_PATH = process.env.PLAYWRIGHT_LIVE_FOCUS_SEED
const FORBIDDEN = /(createAgent|sendAgentPrompt|agent-prompt|transcript|\/agents|\/claim|\/reply)/i
const OUTPUT_BODY = '修复登录失败：保存后刷新必须仍能看到这条 Boss 原文'
const OUTPUT_ACCEPTANCE = '刷新后仍保持登录'
const OUTPUT_CONSTRAINTS = '不要修改现有会话格式'

type Seed = { slug: string; display_name?: string; workspace_id: string }

type Gates = {
  posts: { url: string; body: string }[]
  failed: { url: string; error: string }[]
  forbidden: string[]
  urls: string[]
}

function loadSeed(): Seed {
  if (!SEED_PATH) throw new Error('PLAYWRIGHT_LIVE_FOCUS_SEED is required (see spec header)')
  const seed = JSON.parse(readFileSync(SEED_PATH, 'utf8')) as Seed
  if (typeof seed.slug !== 'string' || typeof seed.workspace_id !== 'string') {
    throw new Error('seed must contain slug and workspace_id')
  }
  return seed
}

function attachGates(page: Page): Gates {
  const gates: Gates = { posts: [], failed: [], forbidden: [], urls: [] }
  page.on('request', (request: Request) => {
    const url = request.url()
    const body = request.postData() ?? ''
    gates.urls.push(url)
    if (['POST', 'PUT', 'PATCH'].includes(request.method())) {
      gates.posts.push({ url, body })
    }
    if (FORBIDDEN.test(url) || FORBIDDEN.test(body)) {
      gates.forbidden.push(`${request.method()} ${url} ${body.slice(0, 200)}`)
    }
  })
  page.on('requestfailed', (request) => {
    gates.failed.push({
      url: request.url(),
      error: request.failure()?.errorText ?? 'requestfailed',
    })
  })
  return gates
}

function persistDiagnostics(gates: Gates) {
  const dir = process.env.PLAYWRIGHT_LIVE_ARTIFACT_DIR
  if (!dir) return
  writeFileSync(
    join(dir, 'checkpoint-a-focus-diagnostics.json'),
    `${JSON.stringify({ posts: gates.posts, failed: gates.failed, forbidden: gates.forbidden }, null, 2)}\n`,
    'utf8',
  )
}

async function expectNoHorizontalOverflow(page: Page, width: number) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `${width}px scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

test('Checkpoint A Local Focus: 保存/刷新主链、零 Agent API、四视口双主题、Files/Terminal 不回归', async ({
  page,
}) => {
  const seed = loadSeed()
  const gates = attachGates(page)
  const homePath = `/projects/${seed.slug}/workspaces/${seed.workspace_id}`
  try {
    await page.setViewportSize({ width: 1280, height: 800 })
    expect(page.url(), 'live base url 不得使用保留端口').not.toMatch(/:8790|:18790/)
    const document = await page.goto('/#/projects')
    expect(document, 'document must come from the live server').not.toBeNull()
    await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
    await expect(
      page.getByRole('link', { name: seed.display_name ?? seed.slug }),
      'seeded active Project 必须出现在项目列表',
    ).toBeVisible()

    // ---- 空态 + 保存主链（键盘输入）----
    await page.goto(`/#${homePath}`)
    const composer = page.getByLabel('今天想推进什么？')
    await expect(composer, 'Focus 空态必须问“今天想推进什么？”').toBeVisible({ timeout: 20_000 })
    await composer.focus()
    await page.keyboard.type(OUTPUT_BODY)
    await page.getByText('怎样算完成？').click()
    await page.getByLabel('怎样算完成？').fill(OUTPUT_ACCEPTANCE)
    await page.getByText('需要特别注意什么？').click()
    await page.getByLabel('需要特别注意什么？').fill(OUTPUT_CONSTRAINTS)
    const save = page.getByRole('button', { name: '保存工作' })
    await expect(save, '唯一主按钮是“保存工作”').toBeEnabled()
    expect(await page.getByRole('button', { name: '保存并开始' }).count()).toBe(0)
    const postsBefore = gates.posts.length
    await save.click()
    await expect(page.getByText('工作已保存')).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible()
    await expect(page.getByText(OUTPUT_ACCEPTANCE)).toBeVisible()
    await expect(page.getByText(OUTPUT_CONSTRAINTS)).toBeVisible()
    const savePosts = gates.posts.slice(postsBefore)
    expect(savePosts, '保存必须恰好 POST 一次 work-items').toHaveLength(1)
    expect(savePosts[0]!.url).toContain('/work-items')
    expect(JSON.parse(savePosts[0]!.body)).toEqual({
      body: OUTPUT_BODY,
      acceptance: OUTPUT_ACCEPTANCE,
      constraints: OUTPUT_CONSTRAINTS,
    })

    // ---- 刷新：同 IDs/原文（GET 回读，不读 transcript）----
    await page.reload()
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText('工作已保存')).toBeVisible()
    await expect(page.getByLabel('今天想推进什么？')).toHaveCount(0)
    expect(gates.posts.length, '刷新不得产生新的写请求').toBe(savePosts.length)

    // ---- 离开再返回 ----
    await page.getByTitle('文件').click()
    await expect(page).toHaveURL(/\/files$/)
    await expect(page.getByText(OUTPUT_BODY)).toHaveCount(0)
    await page.getByTitle('工作对话').click()
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}$`))
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible()
    await expect(page.getByText('工作已保存')).toBeVisible()

    // ---- /agent 深链回到 Focus，无假能力 ----
    await page.goto(`/#${homePath}/agent`)
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible({ timeout: 20_000 })
    expect(await page.getByRole('button', { name: '保存并开始' }).count()).toBe(0)
    for (const absent of ['选择 Agent', '认领', 'Checkout', 'SSH']) {
      expect(await page.getByText(absent).count()).toBe(0)
    }

    // ---- 四视口无横向溢出 + 深浅主题 ----
    for (const width of [1440, 1366, 1280, 390]) {
      await page.setViewportSize({ width, height: 800 })
      await expectNoHorizontalOverflow(page, width)
      await expect(page.getByText(OUTPUT_BODY)).toBeVisible()
    }
    const themeToggle = page.getByRole('button', { name: /切换主题/ })
    await expect(themeToggle).toHaveAccessibleName('切换主题，当前跟随系统')
    await themeToggle.click()
    await expect(page.getByRole('button', { name: '切换主题，当前亮色' })).toBeVisible()
    await themeToggle.click()
    await expect(page.getByRole('button', { name: '切换主题，当前暗色' })).toBeVisible()
    await expect(
      await page.evaluate(() => document.documentElement.dataset.theme),
      '暗色主题必须落到 html[data-theme=dark]',
    ).toBe('dark')
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible()
    await expectNoHorizontalOverflow(page, 390)
    await themeToggle.click()
    await expect(page.getByRole('button', { name: '切换主题，当前跟随系统' })).toBeVisible()

    // ---- 390 触控目标 ≥44px ----
    await page.setViewportSize({ width: 390, height: 844 })
    const firstLink = page.getByTitle('工作对话')
    const firstBox = await firstLink.boundingBox()
    expect(firstBox, '390 主导航触控目标必须有尺寸').not.toBeNull()
    expect(firstBox!.height, '390 主导航触控目标 ≥44px').toBeGreaterThanOrEqual(44)

    // ---- 键盘：正文输入已用 keyboard.type；skip-link Enter 激活聚焦主内容 ----
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.goto(`/#${homePath}`)
    await expect(page.getByText(OUTPUT_BODY)).toBeVisible({ timeout: 20_000 })
    await page.getByRole('button', { name: '跳到主内容' }).focus()
    await expect(page.getByRole('button', { name: '跳到主内容' })).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.locator('#main-content')).toBeFocused()

    // ---- Files / Terminal 深链不回归 ----
    await page.goto(`/#${homePath}/files`)
    await expect(page.locator('#main-content')).toContainText(/文件|暂未/, { timeout: 20_000 })
    await expectNoHorizontalOverflow(page, 1280)
    await page.goto(`/#${homePath}/terminal`)
    await expect(page.locator('#main-content')).toContainText(/终端/, { timeout: 20_000 })
    await expectNoHorizontalOverflow(page, 1280)

    // ---- 零 Agent API / 零 transcript / 零失败请求 ----
    expect(gates.forbidden, '浏览器不得调用 Agent API 或读取 transcript').toEqual([])
    expect(
      gates.posts.filter((post) => !post.url.includes('/work-items')),
      '除 work-items 外不得有任何写请求',
    ).toEqual([])
    const apiFailures = gates.failed.filter((item) => item.url.includes('/api/'))
    expect(apiFailures, '/api 请求不得失败').toEqual([])
  } finally {
    persistDiagnostics(gates)
  }
})
