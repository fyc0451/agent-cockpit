import { expect, test, type Locator, type Page, type Request } from '@playwright/test'

// web tsconfig 的 types 白名单不含 node 模块/全局（见 tsconfig.json）。本文件
// 只读环境变量、不读文件系统，自行做最小结构声明；Playwright 运行时的全局
// process 满足它。诊断证据经 test.info() attachment 输出，不写磁盘。
declare const process: { env: Record<string, string | undefined> }

/**
 * Checkpoint A acceptance-v2 真实浏览器合同（handoff 2026-08-15 23:54「本批唯一范围」）。
 *
 * 本 spec 只消费真实后端，禁止 route.fulfill / mock / fixture 假旅程：
 * 必须设置 PLAYWRIGHT_LIVE_BASE_URL（真实临时 ephemeral server，随机非保留
 * loopback 端口，禁止 8790/18790）与 PLAYWRIGHT_LIVE_FOCUS_SEED（内联 JSON
 * 字符串：用真实 project_registry_store 预先建立的 active
 * Project/RepoLocation/Workspace，含 slug / display_name / workspaces）。
 * 任一未设置时整组 skip，不产出假绿。
 *
 * 运行配方（本文件位于 web/e2e-live，即 playwright.live.config.ts 的 testDir；
 * 该 config 自身会校验 PLAYWRIGHT_LIVE_BASE_URL 存在且非 8790/18790）：
 *   1. runtime_root=$(mktemp -d) && chmod 700 "$runtime_root"
 *   2. scripts/next_ephemeral_server.py --runtime-root "$runtime_root" --source-sha <HEAD>
 *   3. agent_cockpit.project_registry_store 播种 1 个 Project 与 4 个 Workspace
 *      （nav / tasks / mobile / honesty），把 JSON 内联到 env
 *   4. npm --prefix web run build，然后在 web/ 下运行：
 *      PLAYWRIGHT_LIVE_BASE_URL=<base_url> PLAYWRIGHT_LIVE_FOCUS_SEED=<seed> \
 *        npx playwright test -c playwright.live.config.ts checkpoint-a-acceptance-v2.spec.ts
 *
 * 四个用例各自独占一个 Workspace，互不共享状态：任意 workers/parallelism
 * 设置下都安全（playwright.live.config.ts 为 workers=1 串行），不依赖文件顺序。
 *
 * 选择器按最终 hierarchy 合同（550f9e9）分视口取「可见 role」：
 *   - 桌面（>760px）：工作/文件/终端 嵌套在项目树里，用
 *     getByRole('link', { name })；桌面无「工作对话」入口。
 *   - 390px：mobile-only 快捷行是 工作对话/文件/终端，desktop 嵌套项
 *     display:none，同样用 role 名称定位可见项。
 *   - 项目 sheet 的 管理项目/添加项目 在 sheet 打开时可见。
 *
 * 覆盖：
 *   1. 1440：点击左栏「项目」后 URL 与 Project/Workspace/任务上下文不丢；
 *      管理项目、添加项目各自真实可达。
 *   2. 同 Workspace 两任务新建、任务列表选择、合法非默认 work query 刷新保持、
 *      非法 query 归一到最新。
 *   3. 390x844：项目 sheet、工作/文件/终端、关键 rail buttons bbox ≥44px、
 *      无水平溢出。
 *   4. 零 Agent API、保存后任务状态诚实「未分配」。
 */

const BASE_URL = process.env.PLAYWRIGHT_LIVE_BASE_URL
const SEED_JSON = process.env.PLAYWRIGHT_LIVE_FOCUS_SEED
const RESERVED = /:8790|:18790/
const FORBIDDEN = /(createAgent|sendAgentPrompt|agent-prompt|transcript|\/agents|\/claim|\/reply)/i

const NAV_BODY = '验收导航：点「项目」后上下文必须还在'
const FIRST_BODY = '验收任务一：修复登录过期'
const FIRST_ACCEPTANCE = '刷新后仍保持登录'
const SECOND_BODY = '验收任务二：补注册校验'
const SECOND_ACCEPTANCE = '错误邮箱被拒绝'
const HONESTY_BODY = '验收诚实：任务必须显示未分配'

type Seed = {
  slug: string
  display_name?: string
  workspaces: { nav: string; tasks: string; mobile: string; honesty: string }
}

type Gates = {
  posts: { url: string; key: string | null }[]
  forbidden: string[]
  pageErrors: string[]
}

function loadSeed(): Seed {
  if (!SEED_JSON) throw new Error('PLAYWRIGHT_LIVE_FOCUS_SEED is required (see spec header)')
  const seed = JSON.parse(SEED_JSON) as Seed
  const missing = ['nav', 'tasks', 'mobile', 'honesty'].filter(
    (key) => typeof seed.workspaces?.[key as keyof Seed['workspaces']] !== 'string',
  )
  if (typeof seed.slug !== 'string' || missing.length > 0) {
    throw new Error(`seed must contain slug and workspaces.{${missing.join(',')}}`)
  }
  return seed
}

function attachGates(page: Page): Gates {
  const gates: Gates = { posts: [], forbidden: [], pageErrors: [] }
  page.on('request', (request: Request) => {
    const url = request.url()
    const body = request.postData() ?? ''
    if (['POST', 'PUT', 'PATCH'].includes(request.method())) {
      gates.posts.push({ url, key: request.headers()['idempotency-key'] ?? null })
    }
    if (FORBIDDEN.test(url) || FORBIDDEN.test(body)) {
      gates.forbidden.push(`${request.method()} ${url}`)
    }
  })
  page.on('pageerror', (error) => {
    gates.pageErrors.push(String(error))
  })
  return gates
}

function persistDiagnostics(gates: Gates, name: string) {
  test
    .info()
    .attach(name, { body: `${JSON.stringify(gates, null, 2)}\n`, contentType: 'application/json' })
}

async function expectNoHorizontalOverflow(page: Page, label: string) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }))
  expect(sw, `${label} scrollWidth(${sw}) 不得超过 clientWidth(${cw})`).toBeLessThanOrEqual(cw + 1)
}

async function rail(page: Page): Promise<Locator> {
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav).toBeVisible()
  return nav
}

function homePath(seed: Seed, workspaceId: string): string {
  return `/#/projects/${seed.slug}/workspaces/${workspaceId}`
}

async function openHome(page: Page, seed: Seed, workspaceId: string) {
  await page.goto(homePath(seed, workspaceId))
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
}

/** 无论此前有多少任务，都拿到 Composer（空态直出，已有任务走「新建任务」）。 */
async function newTaskAndSave(page: Page, body: string, acceptance: string) {
  const composer = page.getByLabel('今天想推进什么？')
  const create = page.getByRole('button', { name: '新建任务' })
  await expect(
    composer.or(create),
    '空态必须给出 Composer，已有任务时必须仍能新建任务',
  ).toBeVisible({ timeout: 20_000 })
  if (!(await composer.isVisible())) {
    await create.click()
    await expect(composer).toBeVisible()
  }
  await composer.fill(body)
  await page.getByText('怎样算完成？').click()
  await page.getByLabel('怎样算完成？').fill(acceptance)
  await page.getByRole('button', { name: '保存工作' }).click()
  await expect(page.getByText('工作已保存')).toBeVisible({ timeout: 20_000 })
}

function expectGatesClean(gates: Gates) {
  expect(gates.forbidden, '零 Agent API / 零 transcript').toEqual([])
  expect(gates.pageErrors).toEqual([])
}

test.skip(
  !BASE_URL || !SEED_JSON,
  '需要真实临时后端：设置 PLAYWRIGHT_LIVE_BASE_URL 与 PLAYWRIGHT_LIVE_FOCUS_SEED（见文件头配方）',
)

test('acceptance-v2 · 1/4 项目主项不丢上下文，管理/添加项目真实可达 (1440)', async ({ page }) => {
  const seed = loadSeed()
  const gates = attachGates(page)
  try {
    expect(BASE_URL!, 'live base url 不得使用保留端口').not.toMatch(RESERVED)
    await page.setViewportSize({ width: 1440, height: 900 })
    await openHome(page, seed, seed.workspaces.nav)
    await newTaskAndSave(page, NAV_BODY, '点「项目」后本任务仍可见')
    const before = page.url()
    expect(before, '起点必须是 Workspace 深链').toContain(
      `/projects/${seed.slug}/workspaces/${seed.workspaces.nav}`,
    )

    await (await rail(page)).getByRole('button', { name: '项目', exact: true }).click()

    expect(page.url(), '点击「项目」后 URL 必须保持不变').toBe(before)
    await expect(page.getByText(NAV_BODY, { exact: true }), '任务上下文不得丢失').toBeVisible()
    await expect(
      page.getByTitle('切换项目'),
      'Project 上下文不得丢失',
    ).toContainText(seed.display_name ?? seed.slug)
    await expect(page.getByTitle('切换工作空间'), 'Workspace 上下文不得丢失').toBeVisible()
    // 桌面工作区功能入口：树内可见 role=link 工作/文件/终端（550f9e9 合同）。
    const nav = await rail(page)
    await expect(nav.getByRole('link', { name: '工作', exact: true })).toBeVisible()
    await expect(nav.getByRole('link', { name: '文件', exact: true })).toBeVisible()
    await expect(nav.getByRole('link', { name: '终端', exact: true })).toBeVisible()

    // 管理项目：真实导航到项目页（允许在此清空 Workspace 二级上下文）。
    await nav.getByRole('link', { name: '管理项目' }).click()
    await expect(page).toHaveURL(/\/#\/projects\/?$/)
    await expect(page.getByText('选择项目进入概览')).toBeVisible()
    expect(page.getByTitle('切换工作空间'), '项目页不再有 Workspace 上下文').toHaveCount(0)

    // 返回 Workspace：上下文可恢复。
    await page.goBack()
    await expect(page.getByText(NAV_BODY, { exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByTitle('切换工作空间')).toBeVisible()

    // 添加项目：真实向导对话框可达（入口本身会导航到 /projects?wizard=1）。
    const navAfterBack = await rail(page)
    await navAfterBack.getByRole('link', { name: '添加项目' }).click()
    await expect(
      page.getByRole('dialog', { name: '添加项目' }),
      '添加项目必须打开真实向导',
    ).toBeVisible({ timeout: 20_000 })
    await page.goBack()
    await expect(
      page.getByText(NAV_BODY, { exact: true }),
      '回到工作对话后任务上下文仍在',
    ).toBeVisible({ timeout: 20_000 })

    expectGatesClean(gates)
  } finally {
    persistDiagnostics(gates, 'acceptance-v2-context-diagnostics.json')
  }
})

test('acceptance-v2 · 2/4 两任务新建、列表选择、合法/非法 work query', async ({ page }) => {
  const seed = loadSeed()
  const gates = attachGates(page)
  try {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openHome(page, seed, seed.workspaces.tasks)

    // 同一 Workspace 连续新建两项任务。
    await newTaskAndSave(page, FIRST_BODY, FIRST_ACCEPTANCE)
    await newTaskAndSave(page, SECOND_BODY, SECOND_ACCEPTANCE)

    const workPosts = gates.posts.filter((post) => post.url.includes('/work-items'))
    expect(workPosts, '本用例恰好两次 work-items POST').toHaveLength(2)
    expect(workPosts[0]!.key, '第一次保存必须携带非空 Idempotency-Key').toBeTruthy()
    expect(workPosts[1]!.key, '第二次保存必须携带非空 Idempotency-Key').toBeTruthy()
    expect(workPosts[1]!.key, '两次保存 intent key 必须不同').not.toBe(workPosts[0]!.key)

    const taskList = page.locator('.focus-task-list')
    await expect(taskList).toBeVisible()
    const rowFirst = taskList.getByRole('button', { name: new RegExp(FIRST_BODY) })
    const rowSecond = taskList.getByRole('button', { name: new RegExp(SECOND_BODY) })
    await expect(rowFirst).toBeVisible()
    await expect(rowSecond).toBeVisible()

    // 默认选中最新（SECOND）；其 work query 是「归一目标」。
    await expect(page.getByText(SECOND_ACCEPTANCE)).toBeVisible()
    await expect
      .poll(async () => page.url(), { message: '保存后 URL 必须带上 work query' })
      .toContain('work=')
    const latestUrl = page.url()

    // 列表选择：切到非默认任务 FIRST，内容与 URL 同步。
    await rowFirst.click()
    await expect(page.getByText(FIRST_ACCEPTANCE)).toBeVisible()
    await expect(page.getByText(SECOND_ACCEPTANCE)).toHaveCount(0)
    const firstUrl = page.url()
    expect(firstUrl).toContain('work=')
    expect(firstUrl, '选中的必须是非默认（非最新）任务').not.toBe(latestUrl)

    // 合法非默认 work query：刷新后保持同一任务。
    await page.reload()
    await expect(page.getByText(FIRST_ACCEPTANCE)).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(SECOND_ACCEPTANCE)).toHaveCount(0)
    expect(page.url(), '刷新后 work query 必须保持').toBe(firstUrl)

    // 非法 work query：内容与 URL 都归一到最新任务。
    await page.goto(`${homePath(seed, seed.workspaces.tasks)}?work=not-a-real-id`)
    await expect(
      page.getByText(SECOND_BODY, { exact: true }),
      '非法 id 必须回退到最新任务正文',
    ).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(SECOND_ACCEPTANCE), '非法 id 必须回退到最新任务详情').toBeVisible()
    await expect(page.getByText(FIRST_ACCEPTANCE)).toHaveCount(0)
    await expect
      .poll(async () => page.url(), { message: '非法 work id 必须 replace 为最新任务 id' })
      .toBe(latestUrl)

    // 选中态硬门：被选中任务按钮 aria-current=true（最新任务此刻被选中）。
    await expect(rowSecond, '选中任务必须 aria-current=true').toHaveAttribute('aria-current', 'true')

    expectGatesClean(gates)
  } finally {
    persistDiagnostics(gates, 'acceptance-v2-tasks-diagnostics.json')
  }
})

test('acceptance-v2 · 3/4 390x844 项目 sheet、rail 触控目标与无溢出', async ({ page }) => {
  const seed = loadSeed()
  const gates = attachGates(page)
  try {
    await page.setViewportSize({ width: 390, height: 844 })
    await openHome(page, seed, seed.workspaces.mobile)

    // 390 mobile-only 快捷行：工作对话/文件/终端 可见（desktop 嵌套项隐藏）。
    const nav = await rail(page)
    for (const label of ['工作对话', '文件', '终端']) {
      await expect(
        nav.getByRole('link', { name: label, exact: true }),
        `390 下「${label}」必须可见`,
      ).toBeVisible()
    }
    await expectNoHorizontalOverflow(page, '390 首屏')

    // 底栏关键 buttons 触控目标 ≥44x44（sheet 打开前测量，避免嵌套项双匹配）。
    const projectButton = nav.getByRole('button', { name: '项目', exact: true })
    for (const label of ['项目', '工作对话', '文件', '终端']) {
      const target =
        label === '项目'
          ? projectButton
          : nav.getByRole('link', { name: label, exact: true })
      const box = await target.boundingBox()
      expect(box, `390 下「${label}」必须有 bbox`).not.toBeNull()
      expect(box!.height, `390 下「${label}」高度 ≥44px`).toBeGreaterThanOrEqual(44)
      expect(box!.width, `390 下「${label}」宽度 ≥44px`).toBeGreaterThanOrEqual(44)
    }

    // 项目 sheet：点开为真实底部 sheet，含管理/添加项目入口，可关闭。
    await expect(projectButton).toBeVisible()
    await projectButton.click()
    const sheet = page.locator('#rail-project-tree')
    await expect(sheet, '项目必须展开为 sheet').toBeVisible()
    await expect(sheet.getByRole('link', { name: '管理项目' })).toBeVisible()
    await expect(sheet.getByRole('link', { name: '添加项目' })).toBeVisible()
    await expectNoHorizontalOverflow(page, '390 sheet 打开')
    for (const label of ['管理项目', '添加项目']) {
      const box = await sheet.getByRole('link', { name: label }).boundingBox()
      expect(box, `sheet 内「${label}」必须有 bbox`).not.toBeNull()
      expect(box!.height, `sheet 内「${label}」高度 ≥44px`).toBeGreaterThanOrEqual(44)
    }

    // backdrop 的可视区只剩顶部窄条（sheet 覆盖其中心），用「项目」按钮收起。
    await projectButton.click()
    await expect(sheet).toBeHidden()
    await expectNoHorizontalOverflow(page, '390 sheet 关闭')

    expectGatesClean(gates)
  } finally {
    persistDiagnostics(gates, 'acceptance-v2-390-diagnostics.json')
  }
})

test('acceptance-v2 · 4/4 零 Agent API 且任务状态诚实未分配', async ({ page }) => {
  const seed = loadSeed()
  const gates = attachGates(page)
  try {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openHome(page, seed, seed.workspaces.honesty)
    await newTaskAndSave(page, HONESTY_BODY, '状态必须保持未分配')

    // 已保存任务的状态必须诚实显示「未分配」。
    await expect(page.locator('.focus-task-meta').getByText('未分配')).toBeVisible()
    const rows = page.locator('.focus-task-row')
    expect(await rows.count(), '本用例至少保存一项任务').toBeGreaterThanOrEqual(1)
    await expect(rows.filter({ hasText: HONESTY_BODY }).filter({ hasText: '未分配' })).toBeVisible()

    // 无未实现能力的假入口。
    for (const absent of ['保存并开始', '选择 Agent', '认领', 'Checkout', 'SSH']) {
      expect(await page.getByText(absent).count(), `不得出现「${absent}」`).toBe(0)
    }
    expect(
      gates.posts.filter((post) => !post.url.includes('/work-items')),
      '除 work-items 外不得有任何写请求',
    ).toEqual([])
    expectGatesClean(gates)
  } finally {
    persistDiagnostics(gates, 'acceptance-v2-honesty-diagnostics.json')
  }
})
