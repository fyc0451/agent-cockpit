import { expect, test, type Page, type Request, type Route } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

declare const process: { env: Record<string, string | undefined> }

const SOURCES = {
  happy: process.env.LOCAL_WEB_GATE_HAPPY_SOURCE,
  recovery: process.env.LOCAL_WEB_GATE_RECOVERY_SOURCE,
  malformed: process.env.LOCAL_WEB_GATE_MALFORMED_SOURCE,
}
const RECEIPTS_PATH = process.env.LOCAL_WEB_GATE_RECEIPTS

for (const [name, value] of Object.entries(SOURCES)) {
  if (!value) throw new Error(`LOCAL_WEB_GATE_${name.toUpperCase()}_SOURCE is required`)
}
if (!RECEIPTS_PATH) throw new Error('LOCAL_WEB_GATE_RECEIPTS is required')

type PreparationReceipt = {
  preparation_work_item_id: string
  checkout_id: string
  lease_id: string
  identity_id: string
  generation: number
  source_head: string
  source_tree: string
}

type Journey = {
  slug: string
  preparationPosts: Request[]
}

const createdBySlug: Record<string, PreparationReceipt> = {}
let recoveredPreparation: PreparationReceipt | null = null

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`invalid preparation receipt: ${field}`)
  }
  return value as Record<string, unknown>
}

function stringValue(value: unknown, field: string): string {
  if (typeof value !== 'string' || value === '') {
    throw new Error(`invalid preparation receipt: ${field}`)
  }
  return value
}

function preparationReceipt(payload: unknown): PreparationReceipt {
  const envelope = record(payload, 'envelope')
  const data = record(envelope.data, 'data')
  const checkout = record(data.checkout, 'checkout')
  const lease = record(data.lease, 'lease')
  const principal = record(data.principal, 'principal')
  if (typeof principal.generation !== 'number' || !Number.isInteger(principal.generation)) {
    throw new Error('invalid preparation receipt: generation')
  }
  return {
    preparation_work_item_id: stringValue(data.work_item_id, 'work_item_id'),
    checkout_id: stringValue(checkout.checkout_id, 'checkout_id'),
    lease_id: stringValue(lease.lease_id, 'lease_id'),
    identity_id: stringValue(principal.identity_id, 'identity_id'),
    generation: principal.generation,
    source_head: stringValue(checkout.source_head, 'source_head'),
    source_tree: stringValue(checkout.source_tree, 'source_tree'),
  }
}

async function capturePreparation(response: { json(): Promise<unknown> }): Promise<PreparationReceipt> {
  return preparationReceipt(await response.json())
}

async function createProjectWorkspaceTask(
  page: Page,
  sourceName: string,
  slug: string,
  body: string,
): Promise<Journey> {
  const preparationPosts: Request[] = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && /\/preparation$/.test(request.url())) {
      preparationPosts.push(request)
    }
  })

  await page.goto('/#/projects')
  const firstProject = page.getByRole('button', { name: '选择代码目录' })
  const anotherProject = page.getByRole('button', { name: '添加项目' })
  await expect(firstProject.or(anotherProject).first()).toBeVisible()
  await firstProject.or(anotherProject).first().click()

  const projectDialog = page.getByRole('dialog', { name: '添加项目' })
  await expect(projectDialog).toBeVisible()
  const uploadsRoot = projectDialog.getByRole('button', { name: 'uploads', exact: true })
  await expect(uploadsRoot).toBeVisible({ timeout: 20_000 })
  await uploadsRoot.click()
  const source = projectDialog.getByRole('button', { name: new RegExp(`^${sourceName}`) }).first()
  await expect(source).toBeVisible({ timeout: 20_000 })
  await source.click()
  await projectDialog.getByRole('button', { name: '检查并继续' }).click()
  await expect(projectDialog.getByText('新 Git 项目')).toBeVisible({ timeout: 20_000 })
  await projectDialog.getByText('高级选项').click()
  await projectDialog.getByLabel('标识符').fill(slug)
  await projectDialog.getByRole('button', { name: '确认添加' }).click()
  await expect(projectDialog.getByText('添加成功')).toBeVisible({ timeout: 20_000 })
  await projectDialog.getByRole('button', { name: '继续创建工作空间' }).click()

  const workspaceDialog = page.getByRole('dialog', { name: '创建工作空间' })
  await expect(workspaceDialog).toBeVisible({ timeout: 20_000 })
  await workspaceDialog.getByLabel('工作空间名称').fill('release-gate')
  await workspaceDialog.getByRole('button', { name: '创建并打开' }).click()

  const composer = page.getByLabel('今天想推进什么？')
  await expect(composer).toBeVisible({ timeout: 20_000 })
  await composer.fill(body)
  await page.getByText('怎样算完成？').click()
  await page.getByLabel('怎样算完成？').fill('真实浏览器看到已完成，source 仓库保持不变')
  await page.getByText('需要特别注意什么？').click()
  await page.getByLabel('需要特别注意什么？').fill('只允许修改 managed Checkout')
  await page.getByRole('button', { name: '保存工作' }).click()
  await expect(page.getByText('工作已保存')).toBeVisible({ timeout: 20_000 })

  await page.getByLabel('成员名称').fill(`member-${slug}`)
  await page.getByRole('button', { name: '新建成员' }).click()
  await expect(page.getByRole('radio', { name: `member-${slug}` })).toBeChecked()

  return { slug, preparationPosts }
}

async function expectPreparationFailClosed(page: Page) {
  await expect(page.getByText(/错误码：protocol_error/)).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: '连接只读 Agent' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '准备执行' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新建成员' })).toHaveCount(0)
  await expect(page.getByRole('radio')).toHaveCount(0)
}

test.describe.serial('Local Web release gate', () => {
  test.afterAll(async () => {
    expect(Object.keys(createdBySlug).sort()).toEqual([
      'release-gate-happy', 'release-gate-malformed', 'release-gate-recovery',
    ])
    expect(recoveredPreparation).not.toBeNull()
    await writeFile(RECEIPTS_PATH!, JSON.stringify({
      created_by_slug: createdBySlug,
      recovered_preparation: recoveredPreparation,
    }), { encoding: 'utf-8', mode: 0o600 })
  })

  test('真实 UI 到真实 Codex claim/patch/reply/completed', async ({ page }) => {
    test.setTimeout(420_000)
    const journey = await createProjectWorkspaceTask(
      page,
      SOURCES.happy!,
      'release-gate-happy',
      'Create LOCAL_WEB_RELEASE_GATE.txt containing exactly the single line '
        + 'local web release gate, then reply that the task is complete.',
    )

    const prepare = page.getByRole('button', { name: '准备执行' })
    await expect(prepare).toBeEnabled()
    const created = page.waitForResponse((response) => (
      response.request().method() === 'POST' && /\/preparation$/.test(response.url())
    ))
    await prepare.click()
    const createdResponse = await created
    expect(createdResponse.status()).toBe(201)
    createdBySlug[journey.slug] = await capturePreparation(createdResponse)
    await expect(page.getByText(/已准备（独立 Checkout/)).toBeVisible({ timeout: 30_000 })
    expect(journey.preparationPosts).toHaveLength(1)

    await page.reload()
    await expect(page.getByText(/已准备（独立 Checkout/)).toBeVisible({ timeout: 30_000 })
    expect(journey.preparationPosts, '刷新不得再次创建 preparation/Checkout').toHaveLength(1)

    await page.getByRole('button', { name: '连接只读 Agent' }).click()
    await expect(page.getByText(/已连接（只读/)).toBeVisible({ timeout: 90_000 })
    await page.getByRole('button', { name: '派遣任务' }).click()
    await expect(page.getByText('派遣已提交，等待最新状态。')).toBeVisible({ timeout: 90_000 })

    const timeline = page.getByRole('region', { name: '执行时间线' })
    await expect(timeline).toBeVisible({ timeout: 30_000 })
    await expect(timeline.getByText('已完成', { exact: true }).first()).toBeVisible({
      timeout: 360_000,
    })
    await expect(timeline.locator('.execution-timeline-reply')).toHaveCount(1)

    await page.getByRole('button', { name: '断开' }).click()
    await expect(page.getByRole('button', { name: '连接只读 Agent' })).toBeVisible({
      timeout: 60_000,
    })
  })

  test('同步双击只提交一次；后端提交后丢响应由刷新恢复', async ({ page }) => {
    const journey = await createProjectWorkspaceTask(
      page,
      SOURCES.recovery!,
      'release-gate-recovery',
      'Prepare this task, but do not dispatch it.',
    )
    let committed = 0
    const preparationPattern = '**/preparation'
    const dropCommittedResponse = async (route: Route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      const response = await route.fetch()
      expect(response.status()).toBe(201)
      createdBySlug[journey.slug] = await capturePreparation(response)
      committed += 1
      await route.abort('failed')
    }
    await page.route(preparationPattern, dropCommittedResponse)

    const prepare = page.getByRole('button', { name: '准备执行' })
    await prepare.evaluate((element: HTMLElement) => {
      element.click()
      element.click()
    })
    await expect(page.getByRole('alert')).toContainText('当前无法连接服务', { timeout: 30_000 })
    expect(committed).toBe(1)
    expect(journey.preparationPosts, '同步双击必须只有一个 POST').toHaveLength(1)

    await page.unroute(preparationPattern, dropCommittedResponse)
    const recovered = page.waitForResponse((response) => (
      response.request().method() === 'GET' && /\/preparation$/.test(response.url())
    ))
    await page.reload()
    const recoveredResponse = await recovered
    expect(recoveredResponse.status()).toBe(200)
    recoveredPreparation = await capturePreparation(recoveredResponse)
    expect(recoveredPreparation).toEqual(createdBySlug[journey.slug])
    await expect(page.getByText(/已准备（独立 Checkout/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: '连接只读 Agent' })).toBeEnabled()
    expect(journey.preparationPosts, 'authoritative GET 恢复不得重发 POST').toHaveLength(1)
  })

  test('production parser 对三类 malformed 2xx fail-closed', async ({ page }) => {
    const journey = await createProjectWorkspaceTask(
      page,
      SOURCES.malformed!,
      'release-gate-malformed',
      'Prepare this task, but do not dispatch it.',
    )
    const created = page.waitForResponse((response) => (
      response.request().method() === 'POST' && /\/preparation$/.test(response.url())
    ))
    await page.getByRole('button', { name: '准备执行' }).click()
    const createdResponse = await created
    expect(createdResponse.status()).toBe(201)
    createdBySlug[journey.slug] = await capturePreparation(createdResponse)
    await expect(page.getByText(/已准备（独立 Checkout/)).toBeVisible({ timeout: 30_000 })
    expect(journey.preparationPosts).toHaveLength(1)

    const mutations: Array<(lease: Record<string, unknown>) => void> = [
      (lease) => { delete lease.claim_id },
      (lease) => { lease.claim_id = 7 },
      (lease) => { lease.internal_path = '/forbidden/internal/path' },
    ]
    for (const mutate of mutations) {
      let intercepted = 0
      const malformedGet = async (route: Route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        const response = await route.fetch()
        const body = await response.json() as { data: { lease: Record<string, unknown> } }
        mutate(body.data.lease)
        intercepted += 1
        await route.fulfill({
          response,
          contentType: 'application/json',
          body: JSON.stringify(body),
        })
      }
      await page.route('**/preparation', malformedGet)
      await page.reload()
      await expectPreparationFailClosed(page)
      expect(intercepted).toBe(1)
      expect(journey.preparationPosts).toHaveLength(1)
      await page.unroute('**/preparation', malformedGet)
    }

    await page.reload()
    await expect(page.getByText(/已准备（独立 Checkout/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: '连接只读 Agent' })).toBeEnabled()
    expect(journey.preparationPosts, '恢复真实响应不得重复 Checkout').toHaveLength(1)
  })
})
