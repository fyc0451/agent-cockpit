import { render, screen } from '@testing-library/react'
import { StatusState, type StateKind } from '../components/StatusState'

const NINE_KINDS: StateKind[] = [
  'loading',
  'empty',
  'degraded',
  'disconnected',
  'stale',
  'conflict',
  'forbidden',
  'running',
  'partial-failure',
]

describe('G6 公共状态组件（9 类）', () => {
  it.each(NINE_KINDS)('%s 渲染稳定 data-state', (kind) => {
    const { container } = render(<StatusState kind={kind} />)
    expect(container.querySelector(`[data-state="${kind}"]`)).toBeInTheDocument()
  })

  it('loading 使用 role=status + aria-live', () => {
    render(<StatusState kind="loading" />)
    expect(screen.getByRole('status')).toHaveAttribute('data-state', 'loading')
  })

  it('disconnected 为 danger banner + role=alert', () => {
    render(<StatusState kind="disconnected" banner description="后端不可达" />)
    const el = screen.getByRole('alert')
    expect(el).toHaveAttribute('data-state', 'disconnected')
    expect(el.className).toContain('state-banner--danger')
  })

  it('stale 显示上次更新时间', () => {
    render(<StatusState kind="stale" updatedAt="2026-08-12T10:00:00Z" />)
    expect(screen.getByText(/上次更新：2026-08-12T10:00:00Z/)).toBeInTheDocument()
    expect(screen.getByText('数据可能不是最新')).toBeInTheDocument()
  })

  it('forbidden 显示真实原因 + 文档入口链接', () => {
    render(
      <StatusState
        kind="forbidden"
        reason="Memory/Context Pack 规划在 W4 接通"
        docsRoute="#/settings?view=doctor"
      />,
    )
    expect(screen.getByText('Memory/Context Pack 规划在 W4 接通')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看路线图' })).toHaveAttribute(
      'href',
      '#/settings?view=doctor',
    )
  })

  it('empty 渲染 ✓ 图标 + 标题 + 说明 + 主 CTA', () => {
    render(
      <StatusState
        kind="empty"
        title="还没有可汇总的工作"
        description="连接项目后聚合到这里"
        action={{ label: '开始设置', onClick: () => {} }}
      />,
    )
    expect(screen.getByText('还没有可汇总的工作')).toBeInTheDocument()
    expect(screen.getByText('连接项目后聚合到这里')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始设置' })).toBeInTheDocument()
    expect(screen.getByText('✓')).toBeInTheDocument()
  })

  it('conflict 使用 danger 色调', () => {
    const { container } = render(<StatusState kind="conflict" />)
    expect(container.querySelector('.state-icon--danger')).toBeInTheDocument()
  })
})
