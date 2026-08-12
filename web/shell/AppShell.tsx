import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { CommandPalette } from './CommandPalette'
import { ProjectDrawer } from './ProjectDrawer'
import { Rail } from './Rail'
import { TopBar } from './TopBar'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'

export function AppShell({ children }: { children: ReactNode }) {
  const [projectsOpen, setProjectsOpen] = useState(false)
  const [workspacesOpen, setWorkspacesOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)

  const closePalette = useCallback(() => setPaletteOpen(false), [])
  const closeProjects = useCallback(() => setProjectsOpen(false), [])
  const closeWorkspaces = useCallback(() => setWorkspacesOpen(false), [])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        跳到主内容
      </a>
      <Rail />
      <div className="main">
        <TopBar
          onOpenProjects={() => setProjectsOpen(true)}
          onOpenWorkspaces={() => setWorkspacesOpen(true)}
          onOpenPalette={() => setPaletteOpen(true)}
        />
        <main id="main-content" tabIndex={-1} className="page-scroll">
          <div className="page">{children}</div>
        </main>
      </div>
      <ProjectDrawer open={projectsOpen} onClose={closeProjects} />
      <WorkspaceSwitcher open={workspacesOpen} onClose={closeWorkspaces} />
      <CommandPalette open={paletteOpen} onClose={closePalette} />
    </div>
  )
}
