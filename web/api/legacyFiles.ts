// 群聊工作台目录树：legacy 文件接口客户端（裸 JSON，非 G3 envelope；守卫纪律同 legacyHerdr.ts）。

import { ApiError } from './client'
import { legacyPost, legacyDelete } from './legacyHerdr'
import { legacyGet } from './localSlice'

export interface FileRoots {
  roots: string[]
  groups: Record<string, string[]>
}

export interface DirEntry {
  name: string
  type: string // dir | file | 其他（symlink 等，仅展示）
  size: number
  ext: string
}

export interface DirList {
  path: string
  type: string | null // 传入文件路径时为 'file'
  entries: DirEntry[]
}

export interface FileRead {
  path: string
  text: string
  binary: boolean
  size: number
}

export interface SearchResult {
  path: string
  name: string
  type: string
  relative?: string
}

function fail(field: string): never {
  throw new ApiError({
    code: 'protocol_error',
    message: `legacy files 响应必填字段缺失或类型错误：${field}`,
    retryable: false,
  })
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function optStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function optNum(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

export async function fetchFileRoots(): Promise<FileRoots> {
  const raw = await legacyGet('/api/files/roots')
  if (!isObj(raw) || !Array.isArray(raw.roots)) fail('roots')
  const groups: Record<string, string[]> = {}
  if (isObj(raw.groups)) {
    for (const [k, v] of Object.entries(raw.groups)) {
      if (Array.isArray(v)) groups[k] = v.filter((p): p is string => typeof p === 'string')
    }
  }
  return { roots: raw.roots.filter((p): p is string => typeof p === 'string'), groups }
}

export async function fetchDirList(path: string): Promise<DirList> {
  const raw = await legacyGet(`/api/files?path=${encodeURIComponent(path)}`)
  if (!isObj(raw)) fail('list')
  const entries = Array.isArray(raw.entries) ? raw.entries : []
  return {
    path: optStr(raw.path) || path,
    type: typeof raw.type === 'string' ? raw.type : null,
    entries: entries.map((e, i) => {
      if (!isObj(e)) fail(`entries[${i}]`)
      return {
        name: optStr(e.name),
        type: optStr(e.type) || 'file',
        size: optNum(e.size),
        ext: optStr(e.ext),
      }
    }),
  }
}

export async function fetchFileContent(path: string): Promise<FileRead> {
  const raw = await legacyGet(`/api/files/read?path=${encodeURIComponent(path)}`)
  if (!isObj(raw)) fail('read')
  return {
    path: optStr(raw.path) || path,
    text: optStr(raw.text),
    binary: raw.binary === true,
    size: optNum(raw.size),
  }
}

export async function searchFiles(path: string, q: string): Promise<SearchResult[]> {
  const raw = await legacyGet(
    `/api/files/search?path=${encodeURIComponent(path)}&q=${encodeURIComponent(q)}`,
  )
  if (!isObj(raw)) fail('search')
  const rows = Array.isArray(raw.results) ? raw.results : Array.isArray(raw.matches) ? raw.matches : []
  return rows.flatMap((r) => {
    if (!isObj(r)) return []
    const p = optStr(r.path)
    if (!p) return []
    return [{
      path: p,
      name: optStr(r.name) || p.split('/').pop() || p,
      type: optStr(r.type) || 'file',
      relative: optStr(r.relative) || undefined,
    }]
  })
}

export function fileDownloadUrl(path: string): string {
  return `/api/files/download?path=${encodeURIComponent(path)}`
}

export interface BrowseEntry {
  name: string
  path: string
  hidden: boolean
}

export interface BrowseListing {
  path: string
  home: string
  crumbs: Array<{ name: string; path: string }>
  entries: BrowseEntry[]
  truncated: boolean
}

/** 添加工作区挑选器：空路径从 Home 列一层目录（不走访问白名单）。 */
export async function browsePickerDir(path?: string): Promise<BrowseListing> {
  const suffix = path ? `?path=${encodeURIComponent(path)}` : ''
  const raw = await legacyGet(`/api/files/browse${suffix}`)
  if (!isObj(raw) || !Array.isArray(raw.entries) || !Array.isArray(raw.crumbs)) fail('browse')
  return {
    path: optStr(raw.path),
    home: optStr(raw.home),
    crumbs: raw.crumbs.flatMap((c) => {
      if (!isObj(c)) return []
      const p = optStr(c.path)
      if (!p) return []
      return [{ name: optStr(c.name) || p, path: p }]
    }),
    entries: raw.entries.flatMap((e) => {
      if (!isObj(e)) return []
      const p = optStr(e.path)
      if (!p) return []
      return [{ name: optStr(e.name) || p.split('/').pop() || p, path: p, hidden: e.hidden === true }]
    }),
    truncated: raw.truncated === true,
  }
}

/** 添加工作区（自定义根目录），持久化到后端 */
export function addFileRoot(path: string): Promise<FileRoots> {
  return legacyPost('/api/files/roots', { path })
}

/** 移除工作区（仅自定义目录可移除；系统/已注册项目目录后端会拒绝） */
export function removeFileRoot(path: string): Promise<FileRoots> {
  return legacyDelete(`/api/files/roots?path=${encodeURIComponent(path)}`)
}
