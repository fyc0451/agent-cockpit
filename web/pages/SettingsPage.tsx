import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useLegacyEnvCheck } from '../api/localSlice'
import {
  fetchUpgradeStatus,
  fetchVersionInfo,
  startUpgrade,
  upgradeErrorText,
  type UpgradeStatus,
  type VersionInfo,
} from '../api/upgrade'
import { Button } from '../components/Button'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tabs, tabId, tabPanelId } from '../components/Tabs'
import { Tag } from '../components/Tag'
import { useTheme, type ThemePref } from '../state/theme'

const TABS = [
  { key: 'appearance', label: '外观' },
  { key: 'upgrade', label: '升级' },
  { key: 'doctor', label: '环境自检' },
] as const

type TabKey = (typeof TABS)[number]['key']

function normalizeView(view: string | null): TabKey {
  if (view === 'doctor' || view === 'upgrade') return view
  return 'appearance'
}

function AppearanceTab() {
  const theme = useTheme()
  const options: { key: ThemePref; label: string }[] = [
    { key: 'system', label: '跟随系统' },
    { key: 'light', label: '亮色' },
    { key: 'dark', label: '暗色' },
  ]
  return (
    <section className="panel">
      <h2 className="panel-title">外观</h2>
      <div className="theme-options" role="radiogroup" aria-label="主题">
        {options.map((o) => (
          <label key={o.key} className="theme-option">
            <input
              type="radio"
              name="theme"
              value={o.key}
              checked={theme.pref === o.key}
              onChange={() => theme.setPref(o.key)}
            />
            <span>{o.label}</span>
          </label>
        ))}
      </div>
      <p className="list-sub">当前生效：{theme.resolved === 'dark' ? '暗色' : '亮色'}。只影响本机浏览器。</p>
    </section>
  )
}

function DoctorRow({ name, ok, detail }: { name: string; ok: boolean; detail?: string }) {
  return (
    <li className="list-row">
      <div className="list-row-main">
        <span className="ellipsis list-title">{name}</span>
        {detail ? <span className="ellipsis list-sub">{detail}</span> : null}
      </div>
      <Tag tone={ok ? 'success' : 'danger'}>{ok ? 'ok' : 'fail'}</Tag>
    </li>
  )
}

function DoctorTab() {
  const q = useLegacyEnvCheck()
  return (
    <section className="panel">
      <h2 className="panel-title">环境自检</h2>
      {q.isPending ? (
        <StatusState kind="loading" title="正在运行环境检查…" />
      ) : q.isError ? (
        <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <ul className="list">
          <DoctorRow
            name="herdr"
            ok={q.data.herdr.installed}
            detail={q.data.herdr.installed ? q.data.herdr.path : 'Herdr 未运行'}
          />
          {Object.entries(q.data.agents).map(([name, item]) => (
            <DoctorRow
              key={name}
              name={name}
              ok={item.installed}
              detail={item.installed ? item.path : '未安装'}
            />
          ))}
          <DoctorRow
            name="agent_mail"
            ok={q.data.agent_mail.available}
            detail={
              q.data.agent_mail.available
                ? `读 ${q.data.agent_mail.read_available ? '可用' : '不可用'} · 写 ${q.data.agent_mail.write_available ? '可用' : '不可用'}`
                : (q.data.agent_mail.reason ?? 'Agent Mail 不可用')
            }
          />
        </ul>
      )}
    </section>
  )
}

function upgradeCopy(version: VersionInfo | null, status: UpgradeStatus | null): string {
  if (status?.active) {
    const target = status.targetVersion ? `到 ${status.targetVersion}` : ''
    const phase = status.phase ? `（${status.phase}）` : ''
    return `正在升级${target}${phase}`
  }
  if (status?.state === 'succeeded') {
    return status.targetVersion ? `已升级到 ${status.targetVersion}` : '升级已完成'
  }
  if (status?.state === 'failed') {
    return upgradeErrorText(status.errorCode, status.errorMessage)
  }
  if (status?.state === 'retired' || status?.errorCode === 'upgrade_engine_retired') {
    return '旧升级引擎已退役。源码 8790 请用本页一键升级。'
  }
  if (!status?.available && status?.reason) {
    return upgradeErrorText(status.reason)
  }
  if (version?.status === 'update_available' && version.latest) {
    return `发现 ${version.latest}。会拉取官方 tag、构建前端，并重启当前源码 8790，不会切到官方安装包。`
  }
  if (version?.status === 'up_to_date') {
    return '已是最新版本。'
  }
  if (version?.status === 'unavailable') {
    return '暂时读不到 GitHub latest，可稍后重试。'
  }
  return '正在检查版本…'
}

function UpgradeTab() {
  const [version, setVersion] = useState<VersionInfo | null>(null)
  const [status, setStatus] = useState<UpgradeStatus | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const load = async (refresh = false) => {
    setLoadError(null)
    try {
      const [nextVersion, nextStatus] = await Promise.all([
        fetchVersionInfo(refresh),
        fetchUpgradeStatus(),
      ])
      setVersion(nextVersion)
      setStatus(nextStatus)
    } catch (error) {
      setLoadError(error)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  useEffect(() => {
    if (!status?.active) return
    const timer = window.setInterval(() => {
      void load()
    }, 2000)
    return () => window.clearInterval(timer)
  }, [status?.active])

  const canStart =
    !starting &&
    !status?.active &&
    status?.available === true &&
    version?.status === 'update_available'

  const onStart = async () => {
    if (!canStart) return
    setStarting(true)
    setStartError(null)
    try {
      await startUpgrade()
      await load()
    } catch (error) {
      const code = error instanceof ApiError ? error.code : null
      const message = error instanceof ApiError ? error.message : String(error)
      setStartError(upgradeErrorText(code, message))
    } finally {
      setStarting(false)
    }
  }

  return (
    <section className="panel">
      <h2 className="panel-title">一键升级</h2>
      {loadError ? (
        <QueryErrorState error={loadError} onRetry={() => { void load(true) }} />
      ) : (
        <>
          <p className="list-sub">
            当前 {version?.current ?? '…'}
            {version?.latest ? ` · 远程 ${version.latest}` : ''}
          </p>
          <p className="list-sub">{upgradeCopy(version, status)}</p>
          {startError ? <p className="list-sub">{startError}</p> : null}
          <div className="theme-options">
            <Button
              variant="primary"
              disabled={!canStart}
              title={canStart ? undefined : '没有可升级版本，或升级服务不可用'}
              onClick={() => { void onStart() }}
            >
              {status?.active || starting ? '升级中…' : '一键升级'}
            </Button>
            <Button variant="secondary" onClick={() => { void load(true) }}>
              重新检查
            </Button>
          </div>
        </>
      )}
    </section>
  )
}

export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = normalizeView(searchParams.get('view'))

  return (
    <>
      <Tabs
        tabs={TABS}
        active={tab}
        ariaLabel="设置"
        onChange={(key) => setSearchParams(key === 'appearance' ? {} : { view: key })}
      />
      <div role="tabpanel" id={tabPanelId(tab)} aria-labelledby={tabId(tab)}>
        {tab === 'appearance' ? <AppearanceTab /> : null}
        {tab === 'upgrade' ? <UpgradeTab /> : null}
        {tab === 'doctor' ? <DoctorTab /> : null}
      </div>
    </>
  )
}
