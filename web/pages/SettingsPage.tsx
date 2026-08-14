import { useSearchParams } from 'react-router-dom'
import { useSettings } from '../api/hooks'
import { useLegacyEnvCheck } from '../api/localSlice'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { QueryErrorState } from '../components/QueryErrorState'
import { StatusState } from '../components/StatusState'
import { Tabs, tabId, tabPanelId } from '../components/Tabs'
import { Tag } from '../components/Tag'
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
        <pre className="raw-json" tabIndex={0}>{JSON.stringify(q.data?.data ?? {}, null, 2)}</pre>
      )}
      <div className="state-actions">
        <Button variant="primary" disabled title={writeCap.reason ?? '暂不可保存'}>
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
  // 窄 legacy adapter：env-check 是裸 {herdr,agents,agent_mail}（非 G3 envelope）；
  // 源失败/形状不符一律 typed error，不得显示假成功/假空
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

export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = normalizeView(searchParams.get('view'))

  return (
    <>
      <PageHeader title="设置" sub="当前为只读：修改将在后续版本开放" />
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
