import { Link, Navigate, Route, Routes } from 'react-router-dom'
import { GroupChatPage } from '../features/group-chat/GroupChatPage'
import { SettingsPage } from '../pages/SettingsPage'
import { routePatterns, routes } from './routes'

/**
 * 群聊工作台是主界面；仅保留 /settings（环境自检/外观）。
 * 其余旧版页面（项目列表/工作区等）已退役，代码暂留仓库但不再挂载。
 */
export default function App() {
  return (
    <Routes>
      <Route path={routePatterns.chat} element={<GroupChatPage />} />
      <Route
        path={routePatterns.settings}
        element={
          <div className="page-scroll">
            <div className="page">
              <p>
                <Link to={routes.chat()}>← 返回群聊</Link>
              </p>
              <SettingsPage />
            </div>
          </div>
        }
      />
      <Route path="*" element={<Navigate to={routes.chat()} replace />} />
    </Routes>
  )
}
