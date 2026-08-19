import { Navigate, Route, Routes } from 'react-router-dom'
import { GroupChatPage } from '../features/group-chat/GroupChatPage'
import { routePatterns, routes } from './routes'

/**
 * 群聊工作台是主界面；/settings 仍走同一外壳，中栏换成外观/环境自检。
 * 其余旧版页面（项目列表/工作区等）已退役，代码暂留仓库但不再挂载。
 */
export default function App() {
  return (
    <Routes>
      <Route path={routePatterns.chat} element={<GroupChatPage />} />
      <Route path={routePatterns.settings} element={<GroupChatPage />} />
      <Route path="*" element={<Navigate to={routes.chat()} replace />} />
    </Routes>
  )
}
