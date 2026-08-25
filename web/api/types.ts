// 后端载荷的宽容类型：字段全部可选，页面按「有什么渲染什么」处理。
// 额外字段不崩（TS 结构类型天然容忍），缺失字段用 undefined 与空数组区分。

export interface SourceMeta {
  name?: string
  status?: string
  observed_at?: string | null
  reason?: string | null
}

export interface ResponseMeta {
  request_id?: string
  generated_at?: string
  partial?: boolean
  sources?: SourceMeta[]
  capabilities?: Record<string, unknown>
}

export interface Workspace {
  id?: string
  name?: string
  location?: string
  runtime?: string
  branch?: string
  status?: string
  /** SLICE-001：Registry persisted workspace 权威身份与公开 DTO 字段（不含 canonical root） */
  workspace_id?: string
  isolation_kind?: string
  lifecycle?: string
  goal?: string | null
  version?: number
}

export interface Project {
  slug?: string
  name?: string
  branch?: string
  path?: string
  workspaces?: Workspace[]
  /** SLICE-001：Registry 权威 opaque project_id（slug 仅作 URL/legacy 键） */
  project_id?: string
}

export interface AttentionItem {
  id?: string
  kind?: string
  title?: string
  summary?: string
  status?: string
  project?: string
  workspace?: string
  created_at?: string
  url?: string
}

export interface Attention {
  items?: AttentionItem[]
  count?: number
}

export interface Overview {
  projects?: Project[]
  attention?: Attention | AttentionItem[]
  stats?: Record<string, unknown>
}

export interface Workbench {
  project?: Project
  agents?: unknown[]
  tasks?: Task[]
  activity?: unknown[]
  [key: string]: unknown
}

export interface Settings {
  known_agents?: string[]
  installed_agents?: string[]
  enabled_agents?: string[]
  harness?: Record<string, unknown>
  runtime?: Record<string, unknown>
  nodes?: unknown[]
  [key: string]: unknown
}

export interface EnvCheckItem {
  name?: string
  status?: string
  ok?: boolean
  message?: string
  detail?: string
}

export interface EnvCheck {
  checks?: EnvCheckItem[]
  ok?: boolean
  summary?: string
}

export interface HerdrStatus {
  status?: string
  name?: string
  healthy?: boolean
  sessions?: number
  message?: string
}

export interface Task {
  id?: string
  title?: string
  status?: string
  kind?: string
  project?: string
  workspace?: string
  updated_at?: string
}

export interface Tasks {
  items?: Task[]
  tasks?: Task[]
}

export interface FileRoot {
  id?: string
  name?: string
  path?: string
  kind?: string
}

export interface FileRoots {
  roots?: FileRoot[]
  items?: FileRoot[]
}

export interface FileEntry {
  name?: string
  path?: string
  kind?: string
  size?: number
}

export interface FileSearchResult {
  items?: FileEntry[]
  results?: FileEntry[]
}
