// 团队区类型定义

export interface TeamTopic {
  slug: string // project_slug
  name: string // 显示名
  id: number // project_id
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
