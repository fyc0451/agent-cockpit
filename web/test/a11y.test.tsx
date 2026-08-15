import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from '../components/Button'
import { metaOk, REG_P1 } from '../fixtures/api'
import { THEME_STORAGE_KEY } from '../state/theme'
import { renderApp, stubDefaultFetch } from './helpers'

/** 与 styles/global.css `@media (max-width: 760px)` 触控目标块逐字相同。
 *  vitest `css: false` 且 ESM 测例读不到磁盘 CSS；不得伪造 getBoundingClientRect。 */
const NARROW_TOUCH_CSS = `
.rail-item--mobile-core { gap: 4px; padding-inline: 7px; }
.rail-item--mobile-core .rail-mobile-label { display: inline; }
.btn { min-height: 44px; }
.btn--icon { width: 44px; min-height: 44px; flex: none; }
.rail-item { min-height: 44px; min-width: 44px; justify-content: center; }
.topbar-switcher, .topbar-search { min-height: 44px; }
`

function applyNarrowViewport(width = 390) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
  window.matchMedia = ((query: string) => {
    const max = /max-width:\s*(\d+)/.exec(query)
    const min = /min-width:\s*(\d+)/.exec(query)
    const matches = max ? width <= Number(max[1]) : min ? width >= Number(min[1]) : false
    return {
      matches,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }
  }) as typeof window.matchMedia
  const style = document.createElement('style')
  style.setAttribute('data-test-narrow-css', '760')
  style.textContent = NARROW_TOUCH_CSS
  document.head.appendChild(style)
  window.dispatchEvent(new Event('resize'))
  return style
}

const WORK_ITEMS = `/api/projects/${REG_P1}/workspaces/w1/work-items`
const emptyWorkItems = { data: { items: [], next_cursor: null }, meta: metaOk }

const legacyOverrides = {
  '/api/overview': { projects: [], total_unread: 0, total_projects: 0, total_agents: 0, agent_mail: { available: true } },
  '/api/attention': { sessions: [], items: [], count: 0, mail_unread: 0, capabilities: {} },
  '/api/herdr/status': { available: true, binary: '/usr/local/bin/herdr' },
  '/api/settings': { language: 'zh', known_agents: ['claude'], languages: ['zh', 'en'] },
  [WORK_ITEMS]: emptyWorkItems,
}

describe('A11y（item 7）', () => {
  it('skip link（button 形态）聚焦主内容，不改写业务 hash', async () => {
    stubDefaultFetch(legacyOverrides)
    const user = userEvent.setup()
    const { container } = renderApp('/overview')
    const skip = screen.getByRole('button', { name: '跳到主内容' })
    expect(skip.tagName).toBe('BUTTON')
    await user.click(skip)
    const main = container.querySelector('#main-content')
    expect(main).not.toBeNull()
    expect(main).toHaveAttribute('tabindex', '-1')
    expect(document.activeElement).toBe(main)
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
  })

  it('settings tabs：/settings 回到项目列表，不渲染 Harness tab', async () => {
    stubDefaultFetch(legacyOverrides)
    renderApp('/settings')
    expect(await screen.findByText('选择项目进入概览')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Harness / Runtime 与节点' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '外观' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: '环境自检' })).not.toBeInTheDocument()
  })

  it('aria-disabled 按钮可聚焦、aria-describedby 关联 reason 节点、激活无效', async () => {
    const onClick = vi.fn()
    const { container } = render(
      <Button disabled title="工作空间删除暂未开放" onClick={onClick}>
        删除
      </Button>,
    )
    const btn = screen.getByRole('button', { name: '删除' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', '工作空间删除暂未开放')
    const descId = btn.getAttribute('aria-describedby')
    expect(descId).toBeTruthy()
    const desc = container.querySelector(`#${CSS.escape(descId!)}`)
    expect(desc).not.toBeNull()
    expect(desc?.textContent).toContain('工作空间删除暂未开放')
    ;(btn as HTMLElement).focus()
    expect(btn).toHaveFocus()

    const user = userEvent.setup()
    await user.click(btn)
    await user.keyboard('{Enter}')
    await user.keyboard(' ')
    expect(onClick).not.toHaveBeenCalled()
  })

  it('390 核心 Rail 与 Workspace 主导航只保留工作对话、文件、终端（无 Agent）', async () => {
    const style = applyNarrowViewport(390)
    expect(window.innerWidth).toBe(390)
    expect(window.matchMedia('(max-width: 760px)').matches).toBe(true)
    stubDefaultFetch(legacyOverrides)
    renderApp('/projects/p1/workspaces/w1/files')
    const rail = screen.getByRole('navigation', { name: '主导航' })
    await waitFor(() => expect(within(rail).getByTitle('文件')).toBeInTheDocument())

    for (const title of ['项目', '工作对话', '文件', '终端']) {
      const item = within(rail).getByTitle(title)
      expect(item).toHaveClass('rail-item--mobile-core')
      expect(item.querySelector('.rail-mobile-label')).toHaveTextContent(title)
      const box = getComputedStyle(item)
      expect(Number.parseFloat(box.minHeight)).toBeGreaterThanOrEqual(44)
      expect(Number.parseFloat(box.minWidth)).toBeGreaterThanOrEqual(44)
    }
    const themeBtn = screen.getByRole('button', { name: /切换主题/ })
    expect(Number.parseFloat(getComputedStyle(themeBtn).minHeight)).toBeGreaterThanOrEqual(44)
    expect(Number.parseFloat(getComputedStyle(themeBtn).width)).toBeGreaterThanOrEqual(44)
    const projectSwitch = screen.getByTitle('切换项目')
    expect(Number.parseFloat(getComputedStyle(projectSwitch).minHeight)).toBeGreaterThanOrEqual(44)
    expect(within(rail).queryByTitle('Agent')).toBeNull()
    expect(within(rail).queryByTitle('需要你处理')).toBeNull()
    const workspaceSection = within(rail).getByText('当前工作空间').closest('.rail-section')
    expect(workspaceSection).not.toBeNull()
    expect(
      Array.from(workspaceSection!.querySelectorAll<HTMLElement>('.rail-item')).map((item) => item.title),
    ).toEqual(['工作对话', '文件', '终端'])
    style.remove()
  })

  it('TopBar 主题 cycle + localStorage 持久化 + remount 恢复 + 可访问名称', async () => {
    window.localStorage.clear()
    stubDefaultFetch(legacyOverrides)
    const user = userEvent.setup()
    renderApp('/projects')

    const themeBtn = await screen.findByRole('button', { name: '切换主题，当前跟随系统' })
    expect(themeBtn).toHaveAttribute('title', '主题：跟随系统（点击切换）')

    await user.click(themeBtn)
    expect(screen.getByRole('button', { name: '切换主题，当前亮色' })).toBeInTheDocument()
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')

    await user.click(screen.getByRole('button', { name: '切换主题，当前亮色' }))
    expect(screen.getByRole('button', { name: '切换主题，当前暗色' })).toBeInTheDocument()
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')

    cleanup()
    stubDefaultFetch(legacyOverrides)
    renderApp('/projects')
    expect(await screen.findByRole('button', { name: '切换主题，当前暗色' })).toBeInTheDocument()
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})
