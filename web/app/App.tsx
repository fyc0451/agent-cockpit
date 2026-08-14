import { Navigate, Route, Routes } from 'react-router-dom'
import { AgentPage } from '../pages/AgentPage'
import { FilesPage } from '../pages/FilesPage'
import { InboxPage } from '../pages/InboxPage'
import { OverviewPage } from '../pages/OverviewPage'
import { ProjectWorkbenchPage } from '../pages/ProjectWorkbenchPage'
import { ProjectsPage } from '../pages/ProjectsPage'
import { SettingsPage } from '../pages/SettingsPage'
import { TasksPage } from '../pages/TasksPage'
import { TerminalPage } from '../pages/TerminalPage'
import { UnavailableProjectPage, UnavailableWorkspacePage } from '../pages/UnavailableProjectPage'
import { WelcomePage } from '../pages/WelcomePage'
import { WorkspaceHomePage } from '../pages/WorkspaceHomePage'
import { AppShell } from '../shell/AppShell'
import { routePatterns, routes } from './routes'

/**
 * 冻结深链合同（G1）：pattern 与链接构造全部来自 app/routes.ts 单一权威模块。
 * 未知路由重定向 #/overview。
 */
export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to={routes.overview()} replace />} />
        <Route path={routePatterns.overview} element={<OverviewPage />} />
        <Route path={routePatterns.welcome} element={<WelcomePage />} />
        <Route path={routePatterns.projects} element={<ProjectsPage />} />
        <Route path={routePatterns.projectWorkbench} element={<ProjectWorkbenchPage />} />
        <Route
          path={routePatterns.projectMemory}
          element={
            <UnavailableProjectPage title="项目记忆" sub="Memory / Context Pack" capKey="memory.local" />
          }
        />
        <Route
          path={routePatterns.projectRecovery}
          element={
            <UnavailableProjectPage title="变更审核" sub="Recovery / 恢复提醒审核" capKey="recovery.review" />
          }
        />
        <Route
          path={routePatterns.projectActivity}
          element={<UnavailableProjectPage title="动态" sub="项目活动流" capKey="activity.feed" />}
        />
        <Route path={routePatterns.workspaceBase} element={<WorkspaceHomePage />} />
        <Route
          path={routePatterns.workspaceActivity}
          element={<UnavailableWorkspacePage title="动态" sub="Workspace 活动流" capKey="activity.feed" />}
        />
        <Route path={routePatterns.workspaceFiles} element={<FilesPage />} />
        <Route path={routePatterns.workspaceTerminal} element={<TerminalPage />} />
        <Route path={routePatterns.workspaceAgent} element={<AgentPage />} />
        <Route path={routePatterns.workspaceTasks} element={<TasksPage />} />
        <Route
          path={routePatterns.workspaceGit}
          element={<UnavailableWorkspacePage title="Git" sub="版本控制" capKey="git.integration" />}
        />
        <Route
          path={routePatterns.workspaceEditor}
          element={<UnavailableWorkspacePage title="编辑器" sub="内嵌编辑器" capKey="editor.embedded" />}
        />
        <Route
          path={routePatterns.workspaceBrowser}
          element={<UnavailableWorkspacePage title="浏览器" sub="嵌入式浏览器" capKey="browser" />}
        />
        <Route path={routePatterns.inbox} element={<InboxPage />} />
        <Route path={routePatterns.settings} element={<SettingsPage />} />
        <Route path="*" element={<Navigate to={routes.overview()} replace />} />
      </Routes>
    </AppShell>
  )
}
