import { act, fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { Composer } from '../features/group-chat/Composer'

vi.mock('../api/chatSession', () => ({
  fetchChatSkills: async () => Array.from({ length: 40 }, (_, index) => ({
    id: `skill-${index}`,
    label: `Skill ${index}`,
    insert: `请使用 skill-${index}`,
  })),
}))

describe('Composer 附件与 Skill 菜单', () => {
  it('固定显示上传入口，Skill 列表独立滚动', async () => {
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value=""
          onChange={vi.fn()}
          onSend={vi.fn()}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })

    fireEvent.click(screen.getByRole('button', { name: '＋' }))
    expect(screen.getByRole('menuitem', { name: '上传文件' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: '上传图片' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Skills' })).toHaveClass('gc-attach-skills')
    expect(await screen.findByRole('menuitem', { name: 'Skill 39' })).toBeInTheDocument()
  })

  it('IME 组字和组字结束后的同一轮回车都不误发', async () => {
    const onSend = vi.fn()
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value="中文"
          onChange={vi.fn()}
          onSend={onSend}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    const input = screen.getByRole('textbox')

    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 })
    expect(onSend).not.toHaveBeenCalled()

    fireEvent.compositionEnd(input)
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 })
    expect(onSend).not.toHaveBeenCalled()

    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)) })
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 })
    expect(onSend).toHaveBeenCalledOnce()
    expect(onSend).toHaveBeenCalledWith('queue')
  })

  it('粘贴图片交给附件上传，普通文本粘贴不触发上传', async () => {
    const onAttach = vi.fn()
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value=""
          onChange={vi.fn()}
          onSend={vi.fn()}
          onAttach={onAttach}
          disabled={false}
        />,
      )
    })
    fireEvent.click(screen.getByRole('button', { name: '展开输入框' }))
    const input = screen.getByRole('textbox')
    const image = new File(['png'], 'shot.png', { type: 'image/png' })

    fireEvent.paste(input, {
      clipboardData: {
        items: [{ type: 'text/plain', getAsFile: () => null }],
      },
    })
    expect(onAttach).not.toHaveBeenCalled()

    fireEvent.paste(input, {
      clipboardData: {
        items: [{ type: 'image/png', getAsFile: () => image }],
      },
    })
    expect(onAttach).toHaveBeenCalledWith(image)
  })

  it('点击收起的输入预览会展开，失焦不会自动收起', async () => {
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value=""
          onChange={vi.fn()}
          onSend={vi.fn()}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('gc-composer-preview'))
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    fireEvent.blur(screen.getByRole('textbox'))
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '收起输入框' }))
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开输入框' })).toBeInTheDocument()
  })

  it('已有文字时默认展开输入框', async () => {
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value="已输入的草稿"
          onChange={vi.fn()}
          onSend={vi.fn()}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    expect(screen.getByRole('textbox')).toHaveValue('已输入的草稿')
    expect(screen.getByRole('button', { name: '展开输入全文' })).toBeInTheDocument()
  })

  it('长草稿默认折叠，预览截断，点展开再收起', async () => {
    const long = '一、'.repeat(80) + 'GET /v1/video-generations/health 后面还有很多发布说明。'
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value={long}
          onChange={vi.fn()}
          onSend={vi.fn()}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    const preview = screen.getByTestId('gc-composer-preview')
    expect(preview.textContent || '').toMatch(/…$/)
    expect((preview.textContent || '').length).toBeLessThan(long.length)
    fireEvent.click(screen.getByRole('button', { name: '展开输入框' }))
    expect(screen.getByRole('textbox')).toHaveValue(long)
    expect(screen.getByRole('button', { name: '展开输入全文' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '展开输入全文' }))
    expect(screen.getByRole('textbox')).toHaveAttribute('rows', '12')
    fireEvent.click(screen.getByRole('button', { name: '收起输入全文' }))
    expect(screen.getByRole('textbox')).toHaveAttribute('rows', '3')
    fireEvent.click(screen.getByRole('button', { name: '收起输入框' }))
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开输入框' })).toBeInTheDocument()
  })

  it('默认为排队，点打断才立刻投', async () => {
    const onSend = vi.fn()
    await act(async () => {
      render(
        <Composer
          members={[]}
          leader={null}
          value="先记下"
          onChange={vi.fn()}
          onSend={onSend}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    expect(screen.getByRole('radio', { name: '排队' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: '打断' })).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(screen.getByTitle('排队发送（Enter）'))
    expect(onSend).toHaveBeenCalledWith('queue')
    fireEvent.click(screen.getByRole('radio', { name: '打断' }))
    expect(screen.getByRole('radio', { name: '打断' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(screen.getByTitle('立刻打断发送（Enter）'))
    expect(onSend).toHaveBeenCalledWith('interrupt')
  })

  it('@all 候选可用键盘选中，广播前缀不会让无关成员命中', async () => {
    const onChange = vi.fn()
    await act(async () => {
      render(
        <Composer
          members={[
            {
              paneId: '%1', session: 's1', kind: 'codex', name: 'Evelyn', mailName: 'Evelyn',
              status: 'idle', cwd: '/repo', isLeader: true,
            },
            {
              paneId: '%2', session: 's1', kind: 'kimi', name: 'Bob', mailName: 'Bob',
              status: 'idle', cwd: '/repo', isLeader: false,
            },
          ]}
          leader={null}
          value=""
          onChange={onChange}
          onSend={vi.fn()}
          onAttach={vi.fn()}
          disabled={false}
        />,
      )
    })
    fireEvent.click(screen.getByRole('button', { name: '展开输入框' }))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '@ev', selectionStart: 3 } })
    expect(screen.getByRole('option', { name: /所有人/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('option', { name: /Evelyn/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Bob/ })).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 })
    expect(onChange).toHaveBeenLastCalledWith('@all ')
  })
})
