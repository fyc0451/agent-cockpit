import { screen, waitFor } from '@testing-library/react'
import { defaultFetchMap, metaOk, projectP1 } from '../fixtures/api'
import { parseServerCapabilities } from '../state/capabilities'
import { renderApp, stubFetch } from './helpers'

function stubWithProjectCaps(caps: unknown) {
  return stubFetch({
    ...defaultFetchMap(),
    '/api/projects/p1': { data: projectP1, meta: { ...metaOk, capabilities: caps } },
  })
}

describe('meta.capabilities 权威合并层（item 6）', () => {
  it('parseServerCapabilities 宽容解析 boolean 与 object 两种形态', () => {
    expect(parseServerCapabilities(null)).toEqual({})
    expect(parseServerCapabilities('x')).toEqual({})
    const parsed = parseServerCapabilities({
      'files.read': false,
      'terminal.pty': true,
      browser: { available: true, reason: null },
      git: { available: false, reason: '后端标记关闭' },
      junk: 42,
    })
    expect(parsed['files.read'].available).toBe(false)
    expect(parsed['terminal.pty'].available).toBe(true)
    expect(parsed['terminal.pty'].reason).toBeNull()
    expect(parsed['browser'].available).toBe(true)
    expect(parsed['git'].available).toBe(false)
    expect(parsed['git'].reason).toBe('后端标记关闭')
    expect(parsed['junk']).toBeUndefined()
  })

  it('server meta.capabilities 标记 files.read=false → 页面保持关闭', async () => {
    stubWithProjectCaps({ 'files.read': false })
    const { container } = renderApp('/projects/p1/workspaces/w1/files')
    await waitFor(() => {
      expect(container.querySelector('[data-state="forbidden"]')).toBeInTheDocument()
    })
  })

  it('server 声明 terminal.pty 可用 → 控制按钮放开、disconnected banner 消失', async () => {
    stubWithProjectCaps({ 'terminal.pty': { available: true, reason: null } })
    const { container } = renderApp('/projects/p1/workspaces/w1/terminal')
    const interrupt = await screen.findByRole('button', { name: '中断' })
    await waitFor(() => {
      expect(interrupt).not.toHaveAttribute('aria-disabled')
    })
    expect(screen.getByRole('button', { name: '重连' })).not.toHaveAttribute('aria-disabled')
    expect(container.querySelector('[data-state="disconnected"]')).not.toBeInTheDocument()
  })

  it('server 未提及的 key 保持 fail-closed', async () => {
    stubWithProjectCaps({ 'terminal.pty': { available: true, reason: null } })
    const { container } = renderApp('/projects/p1/workspaces/w1')
    const del = await screen.findByRole('button', { name: '删除 Workspace' })
    expect(del).toHaveAttribute('aria-disabled', 'true')
    const cards = Array.from(container.querySelectorAll('.card'))
    const editorCard = cards.find((c) => c.textContent?.includes('编辑器'))
    expect(editorCard).toHaveClass('card--disabled')
  })
})
