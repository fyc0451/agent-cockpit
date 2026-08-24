import { act, fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { Waterfall, type ChatEntry } from '../features/group-chat/Waterfall'

const longText = Array.from(
  { length: 18 },
  (_, index) => `${index + 1}. 第 ${index + 1} 段：GET /v1/video-generations/health 和 docker compose up。`,
).join('\n')

const entry: ChatEntry = {
  id: 'msg_long',
  kind: 'agent',
  paneId: 'w1:p2',
  name: 'DarkGlacier',
  agentKind: 'codex',
  isLeader: true,
  text: longText,
  to: ['我'],
  ts: 1,
}

describe('Waterfall 长文本', () => {
  it('默认折叠，点按钮展开收起', () => {
    render(<Waterfall entries={[entry]} hasSession />)
    expect(screen.getByRole('button', { name: '展开全文' })).toBeInTheDocument()
    expect(screen.queryByText(/第 18 段/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开全文' }))
    expect(screen.getByText(/第 18 段/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.getByRole('button', { name: '展开全文' })).toBeInTheDocument()
  })

  it('自己发的长气泡也有展开全文', () => {
    render(
      <Waterfall
        entries={[{
          id: 'me_long',
          kind: 'me',
          text: longText,
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 1,
        }]}
        hasSession
      />,
    )
    expect(screen.getByRole('button', { name: '展开全文' })).toBeInTheDocument()
  })

  it('我的气泡按类型显示打断或排队', () => {
    render(
      <Waterfall
        entries={[{
          id: 'me_queue',
          kind: 'me',
          text: '忙完再看',
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 1,
          delivery: 'queue',
        }, {
          id: 'me_interrupt',
          kind: 'me',
          text: '先停下来',
          to: ['GrayFalcon'],
          mailTo: ['GrayFalcon'],
          ts: 2,
          delivery: 'interrupt',
        }, {
          id: 'me_old',
          kind: 'me',
          text: '旧消息',
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 3,
        }]}
        hasSession
      />,
    )
    expect(screen.getByText('排队')).toBeInTheDocument()
    expect(screen.getByText('打断')).toBeInTheDocument()
    expect(document.querySelectorAll('.gc-delivery-badge')).toHaveLength(2)
  })

  it('agent 和我的气泡显示来源与私聊徽章，旧消息不补徽章', () => {
    render(
      <Waterfall
        entries={[{
          id: 'me_direct',
          kind: 'me',
          text: '发给指定成员',
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 1,
          source: 'composer',
          direct: true,
        }, {
          ...entry,
          id: 'agent_h5',
          text: '从手机回复',
          source: 'h5',
          direct: true,
        }, {
          ...entry,
          id: 'agent_api',
          text: '从接口回复',
          source: 'api',
        }, {
          ...entry,
          id: 'agent_legacy',
          text: '旧消息',
        }]}
        hasSession
      />,
    )
    expect(document.querySelectorAll('.gc-src-badge')).toHaveLength(3)
    expect(screen.getByText('composer')).toHaveClass('gc-src-badge')
    expect(screen.getByText('h5')).toHaveClass('gc-src-badge')
    expect(screen.getByText('api')).toHaveClass('gc-src-badge')
    expect(document.querySelectorAll('.gc-direct-badge')).toHaveLength(2)
    expect(screen.getAllByText('定向')).toHaveLength(2)
  })

  it('agent 结果在正文上方显示它回复的原消息', () => {
    render(
      <Waterfall
        entries={[{
          ...entry,
          id: 'agent_reply',
          text: '已将完整描述写入终端。',
          replyTo: { id: 'mail:20', text: '描述在回复里' },
        }]}
        hasSession
      />,
    )
    expect(screen.getByText('回复：')).toBeInTheDocument()
    expect(screen.getByText('描述在回复里').closest('.gc-reply-ref')).not.toBeNull()
  })

  it('我的气泡露出排队中/已送达/已读，结论带用时，看现场带未读数', () => {
    render(
      <Waterfall
        entries={[{
          id: 'me_queued',
          kind: 'me',
          text: '刚发出去',
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 1,
          receipt: 'queued',
        }, {
          id: 'me_read',
          kind: 'me',
          text: '已经读了',
          to: ['BrownDesert'],
          mailTo: ['BrownDesert'],
          ts: 2,
          receipt: 'read',
        }, {
          id: 'agent_done',
          kind: 'agent',
          paneId: 'w1:p2',
          name: 'BrownDesert',
          agentKind: 'grok',
          isLeader: true,
          text: '改完了',
          to: ['我'],
          ts: 3,
          durationMs: 125_000,
        }, {
          id: 'typing:w1:p2',
          kind: 'agent',
          paneId: 'w1:p2',
          name: 'BrownDesert',
          agentKind: 'grok',
          isLeader: true,
          text: '改瀑布流 · 13秒',
          to: [],
          ts: 4,
          unread: 2,
        }]}
        hasSession
        onOpenAgent={vi.fn()}
      />,
    )
    expect(screen.getByText('排队中')).toBeInTheDocument()
    expect(screen.getByText('已读')).toBeInTheDocument()
    expect(screen.getByText('用时 2分5秒')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '看现场，2 条未读' })).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('blocked 现场气泡标成等你输入', () => {
    render(
      <Waterfall
        entries={[{
          ...entry,
          id: 'typing:w1:p4',
          text: '等你输入 · 已 10秒',
          waiting: true,
        }]}
        hasSession
        onOpenAgent={vi.fn()}
      />,
    )
    expect(screen.getByText('等你输入')).toBeInTheDocument()
    expect(document.querySelector('.gc-live-badge.is-waiting')).not.toBeNull()
  })

  it('有结论时先露结论，过程默认收起', () => {
    render(
      <Waterfall
        entries={[{
          ...entry,
          id: 'msg_split',
          text: '先对照设置页和 3.0 外壳。\n结论\n截图圈的「← 返回群聊」已去掉。',
        }]}
        hasSession
      />,
    )
    expect(screen.getByText(/返回群聊/)).toBeInTheDocument()
    expect(screen.queryByText(/先对照设置页/)).not.toBeInTheDocument()
    expect(screen.getByText('过程已收起')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开过程' }))
    expect(screen.getByText(/先对照设置页/)).toBeInTheDocument()
  })

  it('展开后按终端结构显示标题和命令块', () => {
    render(<Waterfall entries={[entry]} hasSession />)
    fireEvent.click(screen.getByRole('button', { name: '展开全文' }))
    expect(document.querySelector('.gc-msg-h')).not.toBeNull()
    expect(document.querySelector('.gc-msg-code')).not.toBeNull()
  })

  it('表格包在可横滑容器里', () => {
    render(
      <Waterfall
        entries={[{
          ...entry,
          id: 'msg_table',
          text: '| 产物 | 节点 | 写入缓存 |\n| --- | --- | --- |\n| 角色核心 | C007 | mbti_profile |',
        }]}
        hasSession
      />,
    )
    expect(document.querySelector('.gc-msg-table-wrap')).not.toBeNull()
    expect(document.querySelector('.gc-msg-table-wrap .gc-msg-table')).not.toBeNull()
  })

  it('折行路径可点开', () => {
    const onOpenPath = vi.fn()
    render(
      <Waterfall
        entries={[{
          ...entry,
          id: 'msg_path',
          text: '完整报告见 tools/m2her-verify/handoffs/2026-08-19-app97-\n  case-cf037f65d2704122b195295073564a1a-1022/REPORT.md。',
        }]}
        hasSession
        onOpenPath={onOpenPath}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /REPORT\.md/ }))
    expect(onOpenPath).toHaveBeenCalledWith(
      'tools/m2her-verify/handoffs/2026-08-19-app97-case-cf037f65d2704122b195295073564a1a-1022/REPORT.md',
    )
  })

  it('气泡不再渲染工作区 git 卡片，即使账本还带着旧字段', () => {
    render(
      <Waterfall
        entries={[{
          id: 'msg_git',
          kind: 'agent',
          paneId: 'w1:p2',
          name: 'BrownDesert',
          agentKind: 'grok',
          isLeader: false,
          text: '改完了',
          to: ['我'],
          ts: 1,
          git: { files: 2, stat: ' a.txt | 2 +-\n b.txt | 1 +' },
        }]}
        hasSession
      />,
    )
    expect(screen.queryByText(/改了 .* 个文件/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看 stat' })).not.toBeInTheDocument()
    expect(document.querySelector('.gc-git-card')).toBeNull()
  })
})

const originalScrollHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
const originalClientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')

function stubFlowMetrics(scrollHeight: { current: number }, clientHeight = 400) {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('gc-flow') ? scrollHeight.current : 0
    },
  })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('gc-flow') ? clientHeight : 0
    },
  })
}

describe('Waterfall 进页沉底', () => {
  const many: ChatEntry[] = Array.from({ length: 12 }, (_, index) => ({
    id: `msg_${index}`,
    kind: 'agent' as const,
    paneId: 'w1:p2',
    name: 'BrownDesert',
    agentKind: 'grok',
    isLeader: true,
    text: `第 ${index + 1} 条`,
    to: ['我'],
    ts: index + 1,
  }))

  afterEach(() => {
    vi.unstubAllGlobals()
    if (originalScrollHeight) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', originalScrollHeight)
    if (originalClientHeight) Object.defineProperty(HTMLElement.prototype, 'clientHeight', originalClientHeight)
  })

  it('首屏就把滚动条钉在底部', () => {
    const height = { current: 2400 }
    stubFlowMetrics(height)
    const { container } = render(<Waterfall entries={many} hasSession />)
    const flow = container.querySelector('.gc-flow') as HTMLElement
    expect(flow.scrollTop).toBe(2400)
  })

  it('内容事后撑开仍跟着钉底', async () => {
    const height = { current: 800 }
    stubFlowMetrics(height)
    class FakeRO {
      static cb: ResizeObserverCallback | null = null
      constructor(cb: ResizeObserverCallback) {
        FakeRO.cb = cb
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', FakeRO)
    const { container } = render(<Waterfall entries={many} hasSession />)
    const flow = container.querySelector('.gc-flow') as HTMLElement
    expect(flow.scrollTop).toBe(800)
    height.current = 3600
    await act(async () => {
      FakeRO.cb?.([], {} as ResizeObserver)
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve())
      })
    })
    expect(flow.scrollTop).toBe(3600)
  })

  it('用户上翻看历史时，新消息不抢回底部', () => {
    const height = { current: 2400 }
    stubFlowMetrics(height)
    const { container, rerender } = render(<Waterfall entries={many} hasSession />)
    const flow = container.querySelector('.gc-flow') as HTMLElement
    flow.scrollTop = 120
    fireEvent.scroll(flow)
    rerender(
      <Waterfall
        entries={[...many, {
          id: 'msg_new',
          kind: 'agent',
          paneId: 'w1:p2',
          name: 'BrownDesert',
          agentKind: 'grok',
          isLeader: true,
          text: '新结论',
          to: ['我'],
          ts: 99,
        }]}
        hasSession
      />,
    )
    expect(flow.scrollTop).toBe(120)
    expect(screen.getByRole('button', { name: '↓ 有新消息' })).toBeInTheDocument()
  })
})
