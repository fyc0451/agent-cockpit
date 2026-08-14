import { expect, test } from '@playwright/test'
import { agentMailStatus } from '../fixtures/api'
import { attachGates, expectGatesClean, stubApi } from './helpers'

function overviewProject(id: number, slug: string, humanKey: string) {
  return {
    id, slug, human_key: humanKey, agent_count: 0, active_agent_count: 0,
    message_count: 0, last_activity: null, unread: 0,
  }
}

test('冷启动深链 tasks：内容渲染 + rail/topbar selection 恢复', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/tasks')
  await expect(page.getByText('task-e2e-1')).toBeVisible()
  const rail = page.getByRole('navigation', { name: '主导航' })
  await expect(rail.getByText('当前项目')).toBeVisible()
  await expect(rail.getByText('当前工作空间')).toBeVisible()
  await expect(rail.getByText('Project One')).toBeVisible()
  await expect(page.locator('.topbar')).toContainText('本机工作区')
  expectGatesClean(g)
})

test('刷新恢复同一 Project/Workspace/页面', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/projects/p1/workspaces/w1/tasks')
  await expect(page.getByText('task-e2e-1')).toBeVisible()
  await page.reload()
  await expect(page.getByText('task-e2e-1')).toBeVisible()
  await expect(page.getByRole('navigation', { name: '主导航' }).getByText('Project One')).toBeVisible()
  expectGatesClean(g)
})

test('后退/前进恢复', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page)
  await page.goto('/#/overview')
  await expect(page.locator('.page-title')).toHaveText('需要你处理')

  const rail = page.getByRole('navigation', { name: '主导航' })
  await rail.getByRole('link', { name: '项目' }).click()
  await expect(page.locator('.page-title')).toHaveText('项目')
  expect(page.url()).toContain('#/projects')

  await page.goBack()
  await expect(page.locator('.page-title')).toHaveText('需要你处理')
  await page.goForward()
  await expect(page.locator('.page-title')).toHaveText('项目')
  expectGatesClean(g)
})

test('A→B 同 workspaceId 切换：DOM 无旧 workspace 残留', async ({ page }) => {
  const g = attachGates(page)
  await stubApi(page, {
    '/api/overview': {
      projects: [overviewProject(1, 'p1', '/repos/p1'), overviewProject(2, 'p2', '/repos/p2')],
      total_unread: 0,
      total_projects: 2,
      total_agents: 0,
      agent_mail: agentMailStatus,
    },
    '/api/projects/p2/workbench': {
      project: { id: 8, slug: 'p2', created_at: '2026-08-02T00:00:00+00:00' },
      assignments: [],
      sessions: [],
      source: { available: true, degraded: false, observed_at: '2026-08-13T10:00:00+00:00' },
    },
  })
  await page.goto('/#/projects/p1/workspaces/w1')
  const rail = page.getByRole('navigation', { name: '主导航' })
  await expect(rail.getByText('本机工作区').first()).toBeVisible()

  await page.getByTitle('切换项目').click()
  await page.getByRole('dialog', { name: '项目切换' }).getByRole('button', { name: 'Project Two' }).click()

  await expect(rail.getByText('Project Two')).toBeVisible()
  // A 的 workspace 痕迹（名称/顶栏）必须全部消失
  await expect(page.locator('body')).not.toContainText('本机工作区')
  await expect(page.locator('.topbar')).toContainText('Project Two')
  expectGatesClean(g)
})
