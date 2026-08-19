import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { FilePanel } from '../features/group-chat/FilePanel'
import { FilePreview } from '../features/group-chat/FilePreview'

const writeClipboard = vi.fn()
vi.mock('../features/terminal/termClipboard', () => ({
  writeBrowserClipboard: (...args: unknown[]) => writeClipboard(...args),
}))

const list = vi.fn()
const search = vi.fn()
const read = vi.fn()
const git = vi.fn()

vi.mock('../api/chatSession', () => ({
  fetchSessionDirList: (...args: unknown[]) => list(...args),
  searchSessionFiles: (...args: unknown[]) => search(...args),
  fetchSessionFileContent: (...args: unknown[]) => read(...args),
  fetchSessionGit: (...args: unknown[]) => git(...args),
  sessionFileDownloadUrl: (session: string, path: string) =>
    `/api/chat/sessions/${session}/files/download?path=${encodeURIComponent(path)}`,
  sessionFileRawUrl: (session: string, path: string) =>
    `/api/chat/sessions/${session}/files/raw?path=${encodeURIComponent(path)}`,
}))

describe('FilePanel 群聊附件入口', () => {
  beforeEach(() => {
    list.mockReset()
    search.mockReset()
    git.mockReset()
    git.mockResolvedValue({
      repo: true,
      branch: 'main',
      branches: ['main', 'topic'],
      files: 2,
      stat: ' a.txt | 2 +-\n b.txt | 1 +',
    })
    list.mockImplementation((_session: string, path: string) => {
      if (path.endsWith('/cockpit-inbox')) {
        return Promise.resolve({
          path,
          type: 'dir',
          entries: [{
            name: '1787145431152-8780bf48-image.png',
            type: 'file',
            size: 12,
            ext: 'png',
          }],
        })
      }
      return Promise.resolve({
        path,
        type: 'dir',
        entries: [
          { name: 'cockpit-inbox', type: 'dir', size: 0, ext: '' },
          { name: 'README.md', type: 'file', size: 8, ext: 'md' },
        ],
      })
    })
  })

  it('文件栏顶部先列群聊附件，默认收起，点开后再剥时间戳文件名', async () => {
    const onPreview = vi.fn()
    render(
      <FilePanel
        session="cockpit"
        root="/repo"
        open
        onPreview={onPreview}
        onClose={vi.fn()}
      />,
    )
    expect(await screen.findByRole('region', { name: '群聊附件' })).toBeInTheDocument()
    expect(await screen.findByText('README.md')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /群聊附件/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('image.png')).not.toBeInTheDocument()
    expect(screen.queryByText('cockpit-inbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /群聊附件/ }))
    expect(screen.getByRole('button', { name: /群聊附件/ })).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByText('image.png')).toBeInTheDocument()
    fireEvent.click(screen.getByText('image.png'))
    expect(onPreview).toHaveBeenCalledWith('/repo/cockpit-inbox/1787145431152-8780bf48-image.png')
  })

  it('目录树和附件都能复制完整路径', async () => {
    writeClipboard.mockReset()
    writeClipboard.mockResolvedValue(true)
    render(
      <FilePanel
        session="cockpit"
        root="/repo"
        open
        onPreview={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(await screen.findByText('README.md')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制路径 /repo/README.md' }))
    await waitFor(() => {
      expect(writeClipboard).toHaveBeenCalledWith('/repo/README.md')
    })
    fireEvent.click(screen.getByRole('button', { name: /群聊附件/ }))
    expect(await screen.findByText('image.png')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {
      name: '复制路径 /repo/cockpit-inbox/1787145431152-8780bf48-image.png',
    }))
    await waitFor(() => {
      expect(writeClipboard).toHaveBeenLastCalledWith(
        '/repo/cockpit-inbox/1787145431152-8780bf48-image.png',
      )
    })
    expect(screen.getAllByText('已复制').length).toBeGreaterThan(0)
  })

  it('文件页展示工作区分支，并可展开 stat / diff', async () => {
    git.mockImplementation((_session: string, opts?: { diff?: boolean }) => {
      if (opts?.diff) {
        return Promise.resolve({
          repo: true,
          branch: 'main',
          branches: ['main', 'topic'],
          files: 2,
          stat: ' a.txt | 2 +-\n b.txt | 1 +',
          diff: 'diff --git a/a.txt b/a.txt\n+world',
        })
      }
      return Promise.resolve({
        repo: true,
        branch: 'main',
        branches: ['main', 'topic'],
        files: 2,
        stat: ' a.txt | 2 +-\n b.txt | 1 +',
      })
    })
    render(
      <FilePanel
        session="cockpit"
        root="/repo"
        open
        onPreview={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText('2 个文件有改动')).toBeInTheDocument()
    expect(screen.getByText(/整个工作区相对当前分支/)).toBeInTheDocument()
    expect(screen.queryByText(/a\.txt/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看分支' }))
    expect(screen.getByText(/\* main[\s\S]*topic/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看 stat' }))
    expect(screen.getByText(/a\.txt/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看 diff' }))
    expect(await screen.findByText(/\+world/)).toBeInTheDocument()
    expect(git).toHaveBeenCalledWith('cockpit', { diff: true })
  })
})

describe('FilePreview 截图', () => {
  it('图片走 raw 预览，不读成二进制失败', async () => {
    const path = '/repo/cockpit-inbox/1787145431152-8780bf48-image.png'
    render(<FilePreview session="cockpit" path={path} onClose={vi.fn()} />)
    const image = await screen.findByRole('img', { name: 'image.png' })
    expect(image).toHaveAttribute(
      'src',
      `/api/chat/sessions/cockpit/files/raw?path=${encodeURIComponent(path)}`,
    )
    expect(screen.getByRole('link', { name: '⬇ 下载' })).toHaveAttribute(
      'href',
      `/api/chat/sessions/cockpit/files/download?path=${encodeURIComponent(path)}`,
    )
    expect(read).not.toHaveBeenCalled()
    writeClipboard.mockReset()
    writeClipboard.mockResolvedValue(true)
    fireEvent.click(screen.getByRole('button', { name: `复制路径 ${path}` }))
    await waitFor(() => {
      expect(writeClipboard).toHaveBeenCalledWith(path)
    })
    expect(await screen.findByRole('button', { name: `复制路径 ${path}` })).toHaveTextContent('已复制')
  })
})
