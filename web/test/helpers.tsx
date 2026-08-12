import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import App from '../app/App'
import { defaultFetchMap } from '../fixtures/api'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider } from '../state/selection'
import { ThemeProvider } from '../state/theme'

export interface MockResponseSpec {
  status?: number
  body: unknown
}

export type FetchHandler = (url: string) => MockResponseSpec | undefined

/** 以映射表 mock fetch；未命中的 URL 返回 404 envelope */
export function stubFetch(
  handlerOrMap: FetchHandler | Record<string, unknown>,
): ReturnType<typeof vi.fn> {
  const handler: FetchHandler =
    typeof handlerOrMap === 'function'
      ? handlerOrMap
      : (url) => {
          // 最长前缀匹配，保证 /api/projects/p1/workbench 优先于 /api/projects/p1
          const key = Object.keys(handlerOrMap)
            .filter((k) => url === k || url.startsWith(`${k}?`) || url.startsWith(k))
            .sort((a, b) => b.length - a.length)[0]
          return key ? { body: (handlerOrMap as Record<string, unknown>)[key] } : undefined
        }

  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const spec = handler(url) ?? {
      status: 404,
      body: { error: { code: 'not_found', message: `no mock for ${url}`, retryable: false } },
    }
    const status = spec.status ?? 200
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => spec.body,
    } as Response
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

/** 默认载荷 + 允许覆盖 */
export function stubDefaultFetch(overrides: Record<string, unknown> = {}) {
  return stubFetch({ ...defaultFetchMap(), ...overrides })
}

export function renderApp(initialRoute: string): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const tree: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[initialRoute]}>
          <CapabilitiesProvider>
            <SelectionProvider>
              <App />
            </SelectionProvider>
          </CapabilitiesProvider>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  )
  return render(tree)
}
