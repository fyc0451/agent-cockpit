// 团队管理页：账号审批、邀请码、topic 创建与成员审批。
// 侧栏团队区只保留消息相关功能，管理操作集中在本页（/#/team）。

import { useState, Fragment } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  approveTeamUser,
  createTeamInvitation,
  createTeamProject,
  getTeamInvitation,
  listTeamMembers,
  listTeamProjects,
  listTeamUsers,
  patchTeamMember,
  revokeTeamInvitation,
  setTeamUserStatus,
  teamAuthStatus,
  updateTeamInvitation,
} from '../api/teamAuth'
import { routes } from '../app/routes'
import { Button } from '../components/Button'
import { StatusState } from '../components/StatusState'
import { Tag } from '../components/Tag'
import { writeBrowserClipboard } from '../features/terminal/termClipboard'
import type { TeamMember, TeamUser } from '../features/team/model'

function userStatusLabel(status: string): string {
  return status === 'pending' ? '待批准' : status === 'active' ? '已激活' : status === 'disabled' ? '已停用' : status
}

function userStatusTone(status: string): 'warning' | 'success' | 'neutral' {
  return status === 'pending' ? 'warning' : status === 'active' ? 'success' : 'neutral'
}

function memberStatusLabel(status: string): string {
  return status === 'invited' ? '待审批' : status === 'active' ? '已加入' : status === 'removed' ? '已移除' : status
}

/** 系统账号：团队邀请链接 + 审批 / 停用 / 恢复 */
function AccountSection({ currentUsername }: { currentUsername: string }) {
  const queryClient = useQueryClient()
  const usersQ = useQuery({
    queryKey: ['team-admin-users'],
    queryFn: listTeamUsers,
    refetchInterval: 5_000,
  })
  const invitationQ = useQuery({
    queryKey: ['team-invitation'],
    queryFn: getTeamInvitation,
  })
  const [expiresIn, setExpiresIn] = useState('permanent')
  const [copyNote, setCopyNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const invitation = invitationQ.data
  const inviteUrl = invitation
    ? `${window.location.origin}${window.location.pathname}#${routes.teamInvite(invitation.inviteCode)}`
    : ''
  const expiryValue = expiresIn === 'permanent' ? null : Number(expiresIn)

  const inviteM = useMutation({
    mutationFn: () => createTeamInvitation(expiryValue),
    onSuccess: (created) => {
      queryClient.setQueryData(['team-invitation'], created)
      setCopyNote(null)
      setError(null)
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })
  const expiryM = useMutation({
    mutationFn: () => updateTeamInvitation(expiryValue),
    onSuccess: (updated) => {
      queryClient.setQueryData(['team-invitation'], updated)
      setError(null)
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })
  const revokeM = useMutation({
    mutationFn: revokeTeamInvitation,
    onSuccess: () => {
      queryClient.setQueryData(['team-invitation'], null)
      setCopyNote(null)
      setError(null)
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })
  const statusM = useMutation({
    mutationFn: (vars: { username: string; status: string }) =>
      setTeamUserStatus(vars.username, vars.status),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['team-admin-users'] })
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })
  const approveM = useMutation({
    mutationFn: approveTeamUser,
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['team-admin-users'] })
      void queryClient.invalidateQueries({ queryKey: ['team-projects'] })
      void queryClient.invalidateQueries({ queryKey: ['team-members'] })
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })
  const busy = inviteM.isPending || expiryM.isPending || revokeM.isPending || statusM.isPending || approveM.isPending

  const copyInvitation = async () => {
    const copied = await writeBrowserClipboard(inviteUrl)
    setCopyNote(
      copied
        ? '已复制到剪贴板'
        : '自动复制失败，请选中上方链接手动复制',
    )
  }

  return (
    <section className="panel">
      <h2 className="panel-title">账号管理</h2>
      <p className="list-sub">全团队共用一个邀请链接，不绑定 Topic。成员注册并获批账号后，再申请加入需要的 Topic。</p>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '12px 0' }}>
        <select
          aria-label="邀请链接有效期"
          className="input"
          value={expiresIn}
          onChange={(e) => setExpiresIn(e.target.value)}
          disabled={busy}
        >
          <option value="permanent">永久有效</option>
          <option value="1800">30 分钟</option>
          <option value="3600">1 小时</option>
          <option value="21600">6 小时</option>
          <option value="86400">24 小时</option>
          <option value="259200">3 天</option>
          <option value="604800">7 天</option>
        </select>
        <Button
          variant="primary"
          disabled={busy}
          onClick={() => {
            if (!invitation || window.confirm('重新生成后，旧团队邀请链接会立即失效。继续吗？')) {
              inviteM.mutate()
            }
          }}
        >
          {inviteM.isPending ? '生成中…' : invitation ? '重新生成链接' : '生成团队邀请链接'}
        </Button>
        {invitation && <Button disabled={busy} onClick={() => expiryM.mutate()}>更新有效期</Button>}
        {invitation && (
          <Button
            disabled={busy}
            onClick={() => {
              if (window.confirm('撤销后，当前团队邀请链接会立即停止注册。继续吗？')) {
                revokeM.mutate()
              }
            }}
          >
            撤销链接
          </Button>
        )}
      </div>
      {invitationQ.isPending && <StatusState kind="loading" title="正在读取团队邀请链接…" />}
      {inviteUrl && (
        <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
          <label className="list-sub" htmlFor="team-invitation-url">已生成的团队邀请链接</label>
          <textarea
            id="team-invitation-url"
            aria-label="团队邀请链接"
            className="input"
            readOnly
            rows={3}
            value={inviteUrl}
            onFocus={(event) => event.currentTarget.select()}
            style={{ width: '100%', resize: 'vertical', fontFamily: 'monospace' }}
          />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Button onClick={() => void copyInvitation()}>复制链接</Button>
            <span className="list-sub">
              {invitation?.expiresAt
                ? `有效至 ${new Date(invitation.expiresAt * 1000).toLocaleString()}，已使用 ${invitation.useCount} 次`
                : `永久有效，已使用 ${invitation?.useCount ?? 0} 次`}
            </span>
            {copyNote && <span role="status" className="list-sub">{copyNote}</span>}
          </div>
        </div>
      )}
      {error && <p style={{ color: 'var(--dsw-alias-state-error-primary)' }}>{error}</p>}
      {usersQ.isPending && <StatusState kind="loading" title="正在读取账号…" />}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <tbody>
          {(usersQ.data ?? []).map((user: TeamUser) => {
            const isSelfAdmin = user.username === currentUsername && user.roles.includes('admin')
            return (
              <tr key={user.username} style={{ borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
                <td style={{ padding: '8px 4px' }}>
                  {user.display_name} <span className="list-sub">（{user.username}）</span>
                  {user.requested_project_slug && (
                    <span className="list-sub"> · 申请加入 {user.requested_project_slug}</span>
                  )}
                </td>
                <td style={{ padding: '8px 4px' }}>
                  {user.roles.includes('admin') && <Tag tone="accent">管理员</Tag>}{' '}
                  <Tag tone={userStatusTone(user.status)}>{userStatusLabel(user.status)}</Tag>
                </td>
                <td style={{ padding: '8px 4px', textAlign: 'right' }}>
                  {!isSelfAdmin && user.status === 'pending' && (
                    user.requested_project_slug
                      ? (
                        <Button
                          variant="primary"
                          disabled={busy}
                          onClick={() => approveM.mutate(user.username)}
                        >
                          批准加入
                        </Button>
                      )
                      : (
                        <Button
                          variant="primary"
                          disabled={busy}
                          onClick={() => statusM.mutate({ username: user.username, status: 'active' })}
                        >
                          批准账号
                        </Button>
                      )
                  )}
                  {!isSelfAdmin && user.status === 'active' && (
                    <Button
                      variant="danger"
                      disabled={busy}
                      onClick={() => {
                        if (window.confirm(`停用团队账号 ${user.username}？`)) {
                          statusM.mutate({ username: user.username, status: 'disabled' })
                        }
                      }}
                    >
                      停用
                    </Button>
                  )}
                  {!isSelfAdmin && user.status === 'disabled' && (
                    <Button
                      disabled={busy}
                      onClick={() => statusM.mutate({ username: user.username, status: 'active' })}
                    >
                      恢复
                    </Button>
                  )}
                  {isSelfAdmin && <span className="list-sub">当前账号</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}

/** 单个 topic 的成员列表与审批操作 */
function TopicMemberRows({ slug }: { slug: string }) {
  const queryClient = useQueryClient()
  const membersQ = useQuery({
    queryKey: ['team-members', slug],
    queryFn: () => listTeamMembers(slug),
    refetchInterval: 5_000,
  })
  const [error, setError] = useState<string | null>(null)
  const patchM = useMutation({
    mutationFn: (vars: { humanId: number; patch: { status?: string; role?: string } }) =>
      patchTeamMember(slug, vars.humanId, vars.patch),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['team-members', slug] })
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })

  if (membersQ.isPending) return <StatusState kind="loading" title="正在读取成员…" />
  return (
    <div>
      {error && <p style={{ color: 'var(--dsw-alias-state-error-primary)' }}>{error}</p>}
      {membersQ.data?.length === 0 && <p className="list-sub">暂无成员</p>}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <tbody>
          {(membersQ.data ?? []).map((member: TeamMember) => (
            <tr key={member.human_id} style={{ borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
              <td style={{ padding: '6px 4px' }}>
                {member.display_name || member.mention_handle}{' '}
                <span className="list-sub">@{member.mention_handle}</span>
              </td>
              <td style={{ padding: '6px 4px' }}>
                {member.role === 'admin' && <Tag tone="accent">管理员</Tag>}{' '}
                <Tag tone={member.status === 'invited' ? 'warning' : member.status === 'active' ? 'success' : 'neutral'}>
                  {memberStatusLabel(member.status)}
                </Tag>{' '}
                {member.status === 'active' && (
                  <Tag tone={member.online ? 'success' : 'neutral'}>
                    {member.online ? '在线' : '离线'}
                  </Tag>
                )}
              </td>
              <td style={{ padding: '6px 4px', textAlign: 'right' }}>
                {member.status === 'invited' && (
                  <>
                    <Button
                      variant="primary"
                      disabled={patchM.isPending}
                      onClick={() => patchM.mutate({ humanId: member.human_id, patch: { status: 'active' } })}
                    >
                      批准
                    </Button>{' '}
                    <Button
                      variant="danger"
                      disabled={patchM.isPending}
                      onClick={() => patchM.mutate({ humanId: member.human_id, patch: { status: 'removed' } })}
                    >
                      拒绝
                    </Button>
                  </>
                )}
                {member.status === 'active' && (
                  <>
                    <Button
                      disabled={patchM.isPending}
                      onClick={() =>
                        patchM.mutate({
                          humanId: member.human_id,
                          patch: { role: member.role === 'admin' ? 'member' : 'admin' },
                        })
                      }
                    >
                      {member.role === 'admin' ? '降为成员' : '设为管理员'}
                    </Button>{' '}
                    <Button
                      variant="danger"
                      disabled={patchM.isPending}
                      onClick={() => patchM.mutate({ humanId: member.human_id, patch: { status: 'removed' } })}
                    >
                      移除
                    </Button>
                  </>
                )}
                {member.status === 'removed' && (
                  <Button
                    disabled={patchM.isPending}
                    onClick={() => patchM.mutate({ humanId: member.human_id, patch: { status: 'active' } })}
                  >
                    恢复
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** topic 管理：新建 + 各 topic 的成员审批 */
function TopicSection({ username }: { username: string }) {
  const queryClient = useQueryClient()
  const topicsQ = useQuery({
    queryKey: ['team-projects'],
    queryFn: listTeamProjects,
    refetchInterval: 5_000,
  })
  const [expanded, setExpanded] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const createM = useMutation({
    mutationFn: (topicName: string) => createTeamProject(topicName, username),
    onSuccess: () => {
      setName('')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['team-projects'] })
    },
    onError: (e) => {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))
    },
  })

  return (
    <section className="panel">
      <h2 className="panel-title">Topic 管理</h2>
      <p className="list-sub">topic 名称全局唯一，重名会被拒绝；成员申请加入后在这里审批。</p>
      <form
        style={{ display: 'flex', gap: 8, margin: '12px 0' }}
        onSubmit={(e) => {
          e.preventDefault()
          if (name.trim() && !createM.isPending) createM.mutate(name.trim())
        }}
      >
        <input
          aria-label="topic 名称"
          className="input"
          placeholder="新 topic 名称，不选本机目录"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={createM.isPending}
          style={{ flex: 1 }}
        />
        <Button variant="primary" type="submit" disabled={createM.isPending || !name.trim()}>
          {createM.isPending ? '创建中…' : '新建 topic'}
        </Button>
      </form>
      {error && <p style={{ color: 'var(--dsw-alias-state-error-primary)' }}>{error}</p>}
      {topicsQ.isPending && <StatusState kind="loading" title="正在读取 topic…" />}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <tbody>
          {(topicsQ.data ?? []).map((topic) => (
            <Fragment key={topic.slug}>
              <tr style={{ borderTop: '1px solid var(--dsw-alias-border-l1)' }}>
                <td style={{ padding: '8px 4px' }}>
                  {topic.name} <span className="list-sub">（{topic.slug}）</span>
                </td>
                <td style={{ padding: '8px 4px', textAlign: 'right' }}>
                  <Button onClick={() => setExpanded(expanded === topic.slug ? null : topic.slug)}>
                    {expanded === topic.slug ? '收起成员' : '成员管理'}
                  </Button>
                </td>
              </tr>
              {expanded === topic.slug && (
                <tr>
                  <td colSpan={2} style={{ padding: '4px 4px 12px' }}>
                    <TopicMemberRows slug={topic.slug} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </section>
  )
}

export function TeamAdminPage() {
  const authQ = useQuery({ queryKey: ['team-auth-status'], queryFn: teamAuthStatus })

  if (authQ.isPending) {
    return <StatusState kind="loading" title="正在读取团队登录状态…" />
  }
  if (!authQ.data?.logged_in) {
    return (
      <section className="panel">
        <h2 className="panel-title">团队管理</h2>
        <p className="list-sub">
          尚未登录团队账号。请先到 <Link to={routes.chat()}>群聊页</Link> 左侧团队区登录。
        </p>
      </section>
    )
  }
  const username = authQ.data.username ?? ''
  if (!authQ.data.roles.includes('admin')) {
    return (
      <section className="panel">
        <h2 className="panel-title">团队管理</h2>
        <p className="list-sub">当前账号（{username}）不是系统管理员，仅系统管理员可进行账号与成员审批。</p>
      </section>
    )
  }
  return (
    <div style={{ maxWidth: 760 }}>
      <AccountSection currentUsername={username} />
      <TopicSection username={username} />
    </div>
  )
}
