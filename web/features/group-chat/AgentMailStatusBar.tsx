// Agent Mail 连接状态栏：显示 agent 是否能发送消息到瀑布流

import { useQuery } from '@tanstack/react-query'
import { fetchAgentMailStatus } from '../../api/chatSession'
import type { ChatMember } from './model'

interface AgentMailStatusBarProps {
  session: string
  members: ChatMember[]
}

export function AgentMailStatusBar({ session, members }: AgentMailStatusBarProps) {
  // 只检查有 paneId 的 agent 成员
  const agentMembers = members.filter((m) => m.paneId && m.name !== 'human')

  // 轮询每个 agent 的连接状态
  const statusQueries = agentMembers.map((member) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useQuery({
      queryKey: ['agent-mail-status', session, member.paneId],
      queryFn: () => fetchAgentMailStatus(session, member.paneId!),
      refetchInterval: 15_000, // 每 15 秒检查一次
      enabled: !!member.paneId,
    }),
  )

  // 找出所有未连接的 agent
  const disconnected = agentMembers.filter((_, i) => {
    const status = statusQueries[i]?.data
    return status && !status.connected
  })

  if (disconnected.length === 0) {
    return null // 全部正常，不显示
  }

  return (
    <div className="gc-agent-mail-status-bar" role="alert">
      <span className="gc-agent-mail-status-icon">⚠️</span>
      <span className="gc-agent-mail-status-text">
        {disconnected.length === 1 ? (
          <>
            <strong>{disconnected[0].name}</strong> 未连接到 Agent Mail，消息不会进入瀑布流
          </>
        ) : (
          <>
            <strong>{disconnected.length} 个 agent</strong> 未连接到 Agent Mail
          </>
        )}
      </span>
      <details className="gc-agent-mail-status-details">
        <summary>详情</summary>
        <ul>
          {disconnected.map((member) => {
            const idx = agentMembers.indexOf(member)
            const status = statusQueries[idx]?.data
            const missing = status
              ? Object.entries(status.details)
                  .filter(([, v]) => !v)
                  .map(([k]) => k)
              : []
            return (
              <li key={member.paneId}>
                <strong>{member.name}</strong> ({member.paneId})
                {missing.length > 0 && (
                  <>
                    <br />
                    缺失：{missing.join(', ')}
                  </>
                )}
                {status?.error && (
                  <>
                    <br />
                    错误：{status.error}
                  </>
                )}
              </li>
            )
          })}
        </ul>
      </details>
    </div>
  )
}
