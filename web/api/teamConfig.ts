import { legacyGet } from './localSlice'
import { ApiError } from './client'

export interface TeamConfig {
  hub: string
  team_hub: string
  human_auth: string
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function assertTeamConfig(raw: unknown): TeamConfig {
  if (!isObject(raw)) {
    throw new ApiError({
      code: 'protocol_error',
      message: '团队配置响应格式无效',
      retryable: false,
    })
  }
  const hub = raw.hub
  const teamHub = raw.team_hub
  const humanAuth = raw.human_auth
  if (typeof hub !== 'string' || typeof teamHub !== 'string' || typeof humanAuth !== 'string') {
    throw new ApiError({
      code: 'protocol_error',
      message: '团队配置响应缺少有效字段',
      retryable: false,
    })
  }
  return {
    hub,
    team_hub: teamHub,
    human_auth: humanAuth,
  }
}

export async function fetchTeamConfig(): Promise<TeamConfig> {
  return assertTeamConfig(await legacyGet('/api/agent-mail/config'))
}

export async function saveTeamConfig(config: TeamConfig): Promise<TeamConfig> {
  let response: Response
  try {
    response = await fetch('/api/agent-mail/config', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    })
  } catch {
    throw new ApiError({
      code: 'disconnected',
      message: '无法连接后端服务，请确认开发实例是否运行',
      retryable: true,
    })
  }

  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // legacy endpoint errors below still get a useful HTTP message.
  }
  if (!response.ok) {
    const detail = isObject(body) && typeof body.detail === 'string' ? body.detail : null
    throw new ApiError({
      code: response.status === 401 ? 'unauthenticated' : response.status >= 500 ? 'server_error' : 'http_error',
      message: detail ?? `请求失败（HTTP ${response.status}）`,
      retryable: response.status >= 500,
      status: response.status,
    })
  }
  return assertTeamConfig(body)
}
