import { useSearchParams } from 'react-router-dom'
import { useEnvCheck, useSettings } from '../api/hooks'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tabs, tabId, tabPanelId } from '../components/Tabs'
import { Tag, toneForStatus } from '../components/Tag'
import { useCapability } from '../state/capabilities'
import { useTheme, type ThemePref } from '../state/theme'

const TABS = [
  { key: 'harness', label: 'Harness / Runtime 与节点' },
  { key: 'appearance', label: '外观' },
  { key: 'doctor', label: '环境自检' },
] as const

type TabKey = (typeof TABS)[number]['key']

function normalizeView(view: string | null): TabKey {
  if (view === 'doctor' || view === 'appearance' || view === 'harness') return view
  return 'harness'
}

function HarnessTab() {
  const q = useSettings()
  const writeCap = useCapability('settings.write')
  const catalogCap = useCapability('harnessCatalog')
  return (
    <section className="panel">
      <h2 className="panel-title">Harness / Runtime 与节点</h2>
      {!catalogCap.available ? (
        <StatusState kind="degraded" banner title="Harness 目录不可用" description={catalogCap.reason ?? undefined} />
      ) : null}
      {q.isPending ? (
        <StatusState kind="loading" />
      ) : q.isError ? (
        <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <pre className="raw-json">{JSON.stringify(q.data?.data ?? {}, null, 2)}</pre>
      )}
      <div className="state-actions">
        <Button variant="primary" disabled title={writeCap.reason ?? 'W1 只读'}>
          保存设置
        </Button>
      </div>
    </section>
  )
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
      <p className="list-sub">当前生效：{theme.resolved === 'dark' ? '暗色' : '亮色'}</p>
    </section>
  )
}

function DoctorTab() {
  const q = useEnvCheck()
  return (
    <section className="panel">
      <h2 className="panel-title">环境自检</h2>
      {q.isPending ? (
        <StatusState kind="loading" title="正在运行环境检查…" />
      ) : q.isError ? (
        <QueryErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : (q.data?.data.checks ?? []).length === 0 ? (
        <StatusState kind="empty" title="暂无检查结果" />
      ) : (
        <ul className="list">
          {(q.data?.data.checks ?? []).map((c, i) => {
            const failed = c.ok === false || c.status === 'fail'
            return (
              <li key={c.name ?? i} className="list-row">
                <div className="list-row-main">
                  <span className="ellipsis list-title">{c.name ?? 'check'}</span>
                  {c.message || c.detail ? (
                    <span className="ellipsis list-sub">{c.message ?? c.detail}</span>
                  ) : null}
                </div>
                <Tag tone={toneForStatus(failed ? 'fail' : (c.status ?? 'ok'))}>
                  {c.status ?? (failed ? 'fail' : 'ok')}
                </Tag>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = normalizeView(searchParams.get('view'))

  return (
    <>
      <PageHeader title="设置" sub="W1 只读：写操作将在后续迭代开放" />
      <Tabs
        tabs={TABS}
        active={tab}
        ariaLabel="设置"
        onChange={(key) => setSearchParams(key === 'harness' ? {} : { view: key })}
      />
      <div role="tabpanel" id={tabPanelId(tab)} aria-labelledby={tabId(tab)}>
        {tab === 'harness' ? <HarnessTab /> : tab === 'appearance' ? <AppearanceTab /> : <DoctorTab />}
      </div>
    </>
  )
}
