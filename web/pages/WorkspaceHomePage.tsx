import { Link, useParams } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { Tag, toneForLocation, toneForStatus } from '../components/Tag'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { useCapability, type CapabilityKey } from '../state/capabilities'

interface CardDef {
  label: string
  icon: string
  /** 已接通卡片的深链 builder；null 表示未接通 */
  build: ((project: string, workspace: string) => string) | null
  capKey: CapabilityKey | null
}

const CARDS: CardDef[] = [
  { label: '文件', icon: '🗀', build: routes.workspace.files, capKey: null },
  { label: '终端', icon: '▸', build: routes.workspace.terminal, capKey: null },
  { label: '任务', icon: '☑', build: routes.workspace.tasks, capKey: null },
  { label: '编辑器', icon: '✎', build: null, capKey: 'editor.embedded' },
  { label: '浏览器', icon: '◎', build: null, capKey: 'browser' },
]

function NavCard({
  card,
  projectId,
  workspaceId,
}: {
  card: CardDef
  projectId: string
  workspaceId: string
}) {
  const cap = useCapability(card.capKey ?? 'browser')
  const enabled = card.capKey == null || cap.available
  if (!enabled || card.build == null) {
    const reason = cap.reason ?? '未接通'
    return (
      <div className="card card--disabled" title={reason} aria-disabled="true">
        <span className="card-icon" aria-hidden="true">{card.icon}</span>
        <span className="card-label">{card.label}</span>
        <span className="card-reason ellipsis">{reason}</span>
      </div>
    )
  }
  return (
    <Link className="card" to={card.build(projectId, workspaceId)}>
      <span className="card-icon" aria-hidden="true">{card.icon}</span>
      <span className="card-label">{card.label}</span>
    </Link>
  )
}

function WorkspaceBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const delCap = useCapability('workspace.delete')
  const projectId = project.slug ?? ''
  const workspaceId = workspace.id ?? ''

  return (
    <>
      <PageHeader
        title={workspace.name ?? workspace.id}
        sub={`${project.name ?? project.slug} 的 Workspace`}
        actions={
          <Button variant="danger" disabled title={delCap.reason ?? '未开放'}>
            删除 Workspace
          </Button>
        }
      />
      <section className="panel">
        <h2 className="panel-title">元信息</h2>
        <div className="kv-grid">
          <span className="kv-key">位置</span>
          <span>
            <Tag tone={toneForLocation(workspace.location)}>
              {workspace.location === 'remote' ? '远程' : workspace.location === 'local' ? '本机 Local' : workspace.location ?? '未知'}
            </Tag>
          </span>
          <span className="kv-key">Branch</span>
          <span className="ellipsis">{workspace.branch ?? '—'}</span>
          <span className="kv-key">状态</span>
          <span>
            {workspace.status ? <Tag tone={toneForStatus(workspace.status)}>{workspace.status}</Tag> : '—'}
          </span>
        </div>
      </section>
      <div className="card-grid">
        {CARDS.map((card) => (
          <NavCard key={card.label} card={card} projectId={projectId} workspaceId={workspaceId} />
        ))}
      </div>
    </>
  )
}

export function WorkspaceHomePage() {
  const { projectSlug } = useParams<{ projectSlug: string }>()
  return (
    <ProjectScope slug={projectSlug!}>
      {(project) => (
        <WorkspaceScope project={project}>
          {(workspace) => <WorkspaceBody project={project} workspace={workspace} />}
        </WorkspaceScope>
      )}
    </ProjectScope>
  )
}
