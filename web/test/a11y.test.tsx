import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from '../components/Button'
import { renderApp, stubDefaultFetch } from './helpers'

describe('A11y（item 7）', () => {
  it('skip link 指向 #main-content，主内容区存在且 tabindex=-1', () => {
    stubDefaultFetch()
    const { container } = renderApp('/overview')
    const skip = screen.getByRole('link', { name: '跳到主内容' })
    expect(skip).toHaveAttribute('href', '#main-content')
    const main = container.querySelector('#main-content')
    expect(main).not.toBeNull()
    expect(main).toHaveAttribute('tabindex', '-1')
  })

  it('settings tabs：roving tabindex + ArrowRight/Home/End（激活跟随焦点）', async () => {
    stubDefaultFetch()
    const user = userEvent.setup()
    const { container } = renderApp('/settings')

    const harness = await screen.findByRole('tab', { name: 'Harness / Runtime 与节点' })
    expect(harness).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: '外观' })).toHaveAttribute('tabindex', '-1')
    expect(harness).toHaveAttribute('aria-controls', 'panel-harness')

    await user.click(harness) // 聚焦 active tab
    await user.keyboard('{ArrowRight}')
    const appearance = screen.getByRole('tab', { name: '外观' })
    expect(appearance).toHaveAttribute('aria-selected', 'true')
    expect(appearance).toHaveAttribute('tabindex', '0')
    expect(appearance).toHaveFocus()
    expect(harness).toHaveAttribute('tabindex', '-1')

    await user.keyboard('{End}')
    const doctor = screen.getByRole('tab', { name: '环境自检' })
    expect(doctor).toHaveAttribute('aria-selected', 'true')
    expect(doctor).toHaveFocus()
    expect(container.querySelector('#panel-doctor')).not.toBeNull()

    await user.keyboard('{Home}')
    expect(harness).toHaveAttribute('aria-selected', 'true')
    expect(harness).toHaveFocus()

    // 循环：首 tab 上按 ArrowLeft → 跳到末尾
    await user.keyboard('{ArrowLeft}')
    expect(doctor).toHaveAttribute('aria-selected', 'true')
    expect(doctor).toHaveFocus()
  })

  it('aria-disabled 按钮可聚焦、读屏可读原因、激活无效', async () => {
    const onClick = vi.fn()
    render(
      <Button disabled title="Workspace 删除未开放（W1 只读骨架）" onClick={onClick}>
        删除
      </Button>,
    )
    const btn = screen.getByRole('button', { name: '删除' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', 'Workspace 删除未开放（W1 只读骨架）')
    ;(btn as HTMLElement).focus()
    expect(btn).toHaveFocus()

    const user = userEvent.setup()
    await user.click(btn)
    await user.keyboard('{Enter}')
    await user.keyboard(' ')
    expect(onClick).not.toHaveBeenCalled()
  })
})
