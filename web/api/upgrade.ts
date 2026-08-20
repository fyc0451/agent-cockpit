import { ApiError } from './client'
import { legacyGet } from './localSlice'
import { legacyPost } from './legacyHerdr'

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function asString(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null
}

export interface VersionInfo {
  current: string
  latest: string | null
  status: 'up_to_date' | 'update_available' | 'unavailable'
  latestUrl: string | null
  checkedAt: string | null
}

export interface UpgradeStatus {
  state: string
  engine: string | null
  available: boolean
  active: boolean
  targetVersion: string | null
  fromVersion: string | null
  phase: string | null
  errorCode: string | null
  errorMessage: string | null
  reason: string | null
}

export interface UpgradeStartResult {
  accepted: boolean
  targetVersion: string | null
  jobId: string | null
}

const ERROR_TEXT: Record<string, string> = {
  upgrade_in_progress: '已有升级任务进行中',
  already_current: '已是最新版本',
  release_unavailable: '无法获取或验证官方 Release',
  precheck_dirty: '工作区有未提交改动，已停止升级',
  precheck_disk: '磁盘剩余空间不足',
  precheck_git: '源码目录 git 状态不可用',
  precheck_venv: '虚拟环境不可用',
  precheck_supervisor: '无法确认当前源码服务，拒绝升级',
  lock_failed: '无法获取升级锁，拒绝执行',
  spawn_failed: '无法启动升级执行器',
  edition_unsupported: '当前不是源码版，不能走这一键升级',
  native_layout_required: '官方安装包请用签名升级',
  upgrade_engine_retired: '旧升级引擎已退役',
}

export function upgradeErrorText(code: string | null, fallback?: string | null): string {
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code]
  if (fallback) return fallback
  return '升级失败，请稍后重试'
}

export async function fetchVersionInfo(refresh = false): Promise<VersionInfo> {
  const path = refresh ? '/api/version?refresh=true' : '/api/version'
  const raw = await legacyGet(path)
  if (!isObj(raw)) {
    throw new ApiError({ code: 'protocol_error', message: '版本响应无效', retryable: false })
  }
  const current = isObj(raw.current) ? asString(raw.current.version) : null
  const latest = isObj(raw.latest) ? asString(raw.latest.version) : null
  const status =
    raw.status === 'update_available' || raw.status === 'up_to_date' || raw.status === 'unavailable'
      ? raw.status
      : 'unavailable'
  return {
    current: current ?? '未知',
    latest,
    status,
    latestUrl: isObj(raw.latest) ? asString(raw.latest.url) : null,
    checkedAt: asString(raw.checked_at),
  }
}

export async function fetchUpgradeStatus(): Promise<UpgradeStatus> {
  const raw = await legacyGet('/api/upgrade/status')
  if (!isObj(raw)) {
    throw new ApiError({ code: 'protocol_error', message: '升级状态响应无效', retryable: false })
  }
  return {
    state: asString(raw.state) ?? 'idle',
    engine: asString(raw.engine),
    available: raw.available === true,
    active: raw.active === true,
    targetVersion: asString(raw.target_version),
    fromVersion: asString(raw.from_version),
    phase: asString(raw.phase),
    errorCode: asString(raw.error_code),
    errorMessage: asString(raw.error_message),
    reason: asString(raw.reason),
  }
}

export async function startUpgrade(): Promise<UpgradeStartResult> {
  const raw = await legacyPost<Record<string, never>, unknown>('/api/upgrade', {})
  if (!isObj(raw)) {
    throw new ApiError({ code: 'protocol_error', message: '升级启动响应无效', retryable: false })
  }
  if (raw.accepted === false) {
    const code = asString(raw.reason) ?? 'upgrade_engine_retired'
    throw new ApiError({
      code,
      message: upgradeErrorText(code),
      retryable: false,
      status: 409,
    })
  }
  return {
    accepted: raw.accepted === true,
    targetVersion: asString(raw.target_version),
    jobId: asString(raw.job_id) ?? asString(raw.request_id),
  }
}
