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
  const map: Record<string, unknown> = {
    '/api/auth/status': { required: false, authenticated: true, local_only: true },
    ...defaultFetchMap(),
    ...overrides,
  }
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const pathAndQuery = `${path}${url.search}`
    const key = Object.prototype.hasOwnProperty.call(map, pathAndQuery)
      ? pathAndQuery
      : Object.prototype.hasOwnProperty.call(map, path)
        ? path
        : undefined
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
  consoleErrors: ConsoleError[]
  apiRequests: string[]
  apiFailures: ApiFailure[]
  postRequests: string[]
  wsCount: number
  expectedHttpErrors: ExpectedHttpError[]
}

interface ConsoleError {
  text: string
  url: string
}

export interface ExpectedHttpError {
  url: string
  status: number
  method?: string
}

interface ApiFailure {
  url: string
  status: number
  method: string
}

/** pageerror / console.error / request 计数 / WebSocket 计数，全程开启 */
export function attachGates(page: Page, expectedHttpErrors: ExpectedHttpError[] = []): Gates {
  const g: Gates = {
    pageErrors: [],
    consoleErrors: [],
    apiRequests: [],
    apiFailures: [],
    postRequests: [],
    wsCount: 0,
    expectedHttpErrors,
  }
  page.on('pageerror', (e) => g.pageErrors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error') {
      g.consoleErrors.push({ text: m.text(), url: m.location().url })
    }
  })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/api/')) {
      g.apiRequests.push(u.pathname)
      if (r.method() === 'POST') g.postRequests.push(u.pathname)
    }
  })
  page.on('response', (response) => {
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/') && response.status() >= 400) {
      g.apiFailures.push({
        url: `${url.pathname}${url.search}`,
        status: response.status(),
        method: response.request().method(),
      })
    }
  })
  page.on('websocket', () => {
    g.wsCount += 1
  })
  return g
}

function isExpectedHttpError(error: ConsoleError, expected: ExpectedHttpError): boolean {
  if (!new RegExp(`status of ${expected.status}\\b`).test(error.text)) return false
  try {
    const url = new URL(error.url)
    return `${url.pathname}${url.search}` === expected.url
  } catch {
    return false
  }
}

export function expectGatesClean(g: Gates) {
  const actualFailures = g.apiFailures
    .map(({ method, url, status }) => `${method} ${url} ${status}`)
    .sort()
  const expectedFailures = g.expectedHttpErrors
    .map(({ method = 'GET', url, status }) => `${method} ${url} ${status}`)
    .sort()
  expect(actualFailures, 'API failure 集合必须与测试声明精确一致').toEqual(expectedFailures)
  const filtered = g.consoleErrors.filter(
    (error) => !g.expectedHttpErrors.some((expected) => isExpectedHttpError(error, expected)),
  )
  expect(filtered, 'console.error 应为空').toEqual([])
  expect(g.pageErrors, 'pageerror 应为空').toEqual([])
}

export function apiCalls(g: Gates, prefix: string): string[] {
  return g.apiRequests.filter((u) => u.startsWith(prefix))
}
