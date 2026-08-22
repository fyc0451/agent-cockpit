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
}

export interface TeamSessionCandidate {
  name: string // Session 名
  label: string // 显示标签
  generation: number
}

export interface TeamUser {
  username: string
  display_name: string
  roles: string[] // 含 admin 即系统管理员
  status: string // pending | active | disabled
}

export interface TeamMember {
  human_id: number
  display_name: string
  mention_handle: string
  role: string // admin | member
  status: string // invited | active | removed
}
