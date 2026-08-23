// 团队区类型定义

export interface TeamTopic {
  slug: string // project_slug
  name: string // 显示名
  id: number // project_id
  membership?: {
    role: string // admin | member
    status: string // invited | active | removed
    mention_handle: string
  } | null
}

export interface TeamBinding {
  project_slug: string
  session: string // 本机 Session 名
  active?: boolean
  ready?: boolean
  reason?: string | null
  projectRef?: string | null
  replyMode?: 'confirm' | 'auto'
}

export interface TeamReplyRequest {
  inboxItemId: number
  messageId: number
  status: 'awaiting_confirmation' | 'queued' | 'processing' | 'replied' | 'ignored'
  decision: 'approved' | 'auto' | 'ignored' | null
  decidedAt: string | null
}

export interface TeamSessionCandidate {
  name: string // Session 名
  label: string // 显示标签
  status: string
  agentCount: number
  ready: boolean
  reason: string | null
  leadName: string | null
  projectRef: string | null
}

export interface TeamUser {
  subject?: string
  username: string
  display_name: string
  roles: string[] // 含 admin 即系统管理员
  status: string // pending | active | disabled
  requested_project_slug?: string | null
}

export interface TeamMember {
  human_id: number
  display_name: string
  mention_handle: string
  role: string // admin | member
  status: string // invited | active | removed
}
