import { Navigate, Route, Routes } from 'react-router-dom'
import { GroupChatPage } from '../features/group-chat/GroupChatPage'
import { routePatterns, routes } from './routes'

/**
 * 群聊工作台是主界面；/settings 仍走同一外壳，中栏换成外观/升级/环境自检。
 * 其余旧版页面已删除，未知路径回落到 /chat。
 */
export default function App() {
  return (
    <Routes>
      <Route path={routePatterns.chat} element={<GroupChatPage />} />
      <Route path={routePatterns.settings} element={<GroupChatPage />} />
      <Route path={routePatterns.team} element={<GroupChatPage />} />
      <Route path="*" element={<Navigate to={routes.chat()} replace />} />
    </Routes>
  )
}
