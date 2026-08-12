import { expect, type Page } from '@playwright/test'
import { defaultFetchMap } from '../fixtures/api'

/** 覆盖值可以用 { __status, __payload } 形态模拟非 200 / 错误 envelope */
export interface OverrideSpec {
  __status: number
  __payload: unknown
}

function isOverrideSpec(v: unknown): v is OverrideSpec {
  return typeof v === 'object' && v !== null && '__status' in v
}

/** 拦截所有 /api/**：fixture 复刻完整 G3 envelope，未命中的路径回 404 envelope */
export async function stubApi(page: Page, overrides: Record<string, unknown> = {}) {
  const map: Record<string, unknown> = { ...defaultFetchMap(), ...overrides }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const key = Object.keys(map)
      .filter((k) => path === k || path.startsWith(k))
      .sort((a, b) => b.length - a.length)[0]
    if (!key) {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'not_found', message: `no mock for ${path}`, retryable: false },
        }),
      })
    }
    const v = map[key]
    if (isOverrideSpec(v)) {
      return route.fulfill({
        status: v.__status,
        contentType: 'application/json',
        body: JSON.stringify(v.__payload),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(v),
    })
  })
}

export interface Gates {
  pageErrors: string[]
  consoleErrors: string[]
  apiRequests: string[]
  postRequests: string[]
  wsCount: number
}

/** pageerror / console.error / request 计数 / WebSocket 计数，全程开启 */
export function attachGates(page: Page): Gates {
  const g: Gates = { pageErrors: [], consoleErrors: [], apiRequests: [], postRequests: [], wsCount: 0 }
  page.on('pageerror', (e) => g.pageErrors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error') g.consoleErrors.push(m.text())
  })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/api/')) {
      g.apiRequests.push(u.pathname)
      if (r.method() === 'POST') g.postRequests.push(u.pathname)
    }
  })
  page.on('websocket', () => {
    g.wsCount += 1
  })
  return g
}

// console.error 白名单（新增条目必须注释理由）：
// - 状态测试故意模拟 4xx/5xx API 错误（transport_lost/conflict/data_stale），浏览器会把
//   失败响应的网络加载记为 console.error，与应用层处理无关；应用层表现由 data-state 断言。
const CONSOLE_WHITELIST: RegExp[] = [
  /Failed to load resource: the server responded with a status of \d{3}/,
]

export function expectGatesClean(g: Gates) {
  const filtered = g.consoleErrors.filter((t) => !CONSOLE_WHITELIST.some((w) => w.test(t)))
  expect(filtered, 'console.error 应为空').toEqual([])
  expect(g.pageErrors, 'pageerror 应为空').toEqual([])
}

export function apiCalls(g: Gates, prefix: string): string[] {
  return g.apiRequests.filter((u) => u.startsWith(prefix))
}
