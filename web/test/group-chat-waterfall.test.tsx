import { fireEvent, render, screen } from '@testing-library/react'
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
})
