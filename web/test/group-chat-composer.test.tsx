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
  })
})
