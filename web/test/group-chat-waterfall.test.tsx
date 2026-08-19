import { fireEvent, render, screen } from '@testing-library/react'
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
})
