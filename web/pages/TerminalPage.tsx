import { useEffect, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import type { Project, Workspace } from '../api/types'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { StatusState } from '../components/StatusState'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { useCapability } from '../state/capabilities'

function TerminalSurface() {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!containerRef.current) return
    // W1：仅挂载真实 xterm 实例，不连 WebSocket，不写任何模拟输出
    const term = new Terminal({
      fontSize: 11,
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      cursorBlink: false,
      disableStdin: true,
      theme: {
        background: '#0c121b',
        foreground: '#cdd7e6',
        cursor: '#cdd7e6',
        blue: '#77bdf0',
        green: '#61c997',
        yellow: '#e3b469',
        brightBlack: '#66788f',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)
    fit.fit()
    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      term.dispose()
    }
  }, [])

  return <div ref={containerRef} className="terminal-surface" data-testid="terminal-surface" />
}

function TerminalBody({ workspace }: { project: Project; workspace: Workspace }) {
  const ptyCap = useCapability('terminal.pty')
  const btnTitle = (action: string) =>
    ptyCap.available ? undefined : `${ptyCap.reason ?? 'PTY 未接通'}（${action}不可用）`

  return (
    <>
      <PageHeader
        title="终端"
        sub={workspace.name ?? workspace.id}
        actions={
          <>
            <Button variant="secondary" disabled={!ptyCap.available} title={btnTitle('中断')}>
              中断
            </Button>
            <Button variant="secondary" disabled={!ptyCap.available} title={btnTitle('重连')}>
              重连
            </Button>
            <Button variant="danger" disabled={!ptyCap.available} title={btnTitle('重启')}>
              重启
            </Button>
          </>
        }
      />
      {ptyCap.available ? (
        <StatusState
          kind="degraded"
          banner
          title="PTY 已由服务端 capability 标记为可用"
          description="W1 仍未连接真实 PTY 流，终端外壳保持只读展示。"
        />
      ) : (
        <StatusState
          kind="disconnected"
          banner
          title="PTY 未接通"
          description={ptyCap.reason ?? 'W1 仅终端外壳，不写任何假输出。'}
          docsRoute={ptyCap.docsRoute}
        />
      )}
      <TerminalSurface />
    </>
  )
}

export function TerminalPage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <TerminalBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
