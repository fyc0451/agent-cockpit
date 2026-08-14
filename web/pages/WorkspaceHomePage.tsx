import { Link, useParams } from 'react-router-dom'
import type { Project, Workspace } from '../api/types'
import { routes } from '../app/routes'
import { Button } from '../components/Button'
import { PageHeader } from '../components/PageHeader'
import { Tag, toneForLocation, toneForStatus } from '../components/Tag'
import { ProjectScope } from '../features/ProjectScope'
import { WorkspaceScope } from '../features/WorkspaceScope'
import { useCapability, workspaceScope } from '../state/capabilities'

function ToolCard({ to, icon, label }: { to: string; icon: string; label: string }) {
  return (
    <Link className="card" to={to}>
      <span className="card-icon" aria-hidden="true">{icon}</span>
      <span className="card-label">{label}</span>
    </Link>
  )
}

function UnavailableTool({ label, reason }: { label: string; reason: string }) {
  return (
    <div className="card card--disabled workspace-secondary-item" aria-disabled="true">
      <span className="card-label">{label}</span>
      <span className="card-reason">{reason}</span>
    </div>
  )
}

function WorkspaceBody({ project, workspace }: { project: Project; workspace: Workspace }) {
  const projectSlug = project.slug ?? ''
  const workspaceId = workspace.id ?? ''
  const scope = workspaceScope(projectSlug, workspaceId)
  const filesCap = useCapability('files.read', scope)
  const terminalCap = useCapability('terminal.pty', scope)
  const editorCap = useCapability('editor.embedded', scope)
  const browserCap = useCapability('browser', scope)
  const delCap = useCapability('workspace.delete', scope)

  return (
    <>
      <PageHeader title={workspace.name ?? workspace.id} sub={`${project.name ?? project.slug} 的工作空间`} />
      <Link className="card card--primary" to={routes.workspace.agent(projectSlug, workspaceId)}>
        <span className="card-label">开始任务</span>
        <span className="card-reason">选择已安装的 Agent，交代任务并在这里查看回复</span>
      </Link>
      <div className="card-grid workspace-primary-actions">
        {filesCap.available ? (
          <ToolCard to={routes.workspace.files(projectSlug, workspaceId)} icon="🗀" label="文件" />
        ) : null}
        {terminalCap.available ? (
          <ToolCard to={routes.workspace.terminal(projectSlug, workspaceId)} icon="▸" label="终端" />
        ) : null}
      </div>

      <section className="panel">
        <h2 className="panel-title">工作空间信息</h2>
        <div className="kv-grid">
          <span className="kv-key">位置</span>
          <span>
            <Tag tone={toneForLocation(workspace.location)}>
              {workspace.location === 'remote' ? '远程' : '本机'}
            </Tag>
          </span>
          <span className="kv-key">状态</span>
          <span>
            {workspace.status ? <Tag tone={toneForStatus(workspace.status)}>{workspace.status}</Tag> : '—'}
          </span>
        </div>
      </section>

      <details className="workspace-secondary">
        <summary>其他能力</summary>
        <p className="list-sub">编辑器、浏览器、通信与连接尚未开放。</p>
        {!filesCap.available ? (
          <UnavailableTool label="文件" reason={filesCap.reason ?? '文件能力尚未开放'} />
        ) : null}
        {!terminalCap.available ? (
          <UnavailableTool label="终端" reason={terminalCap.reason ?? '终端能力尚未开放'} />
        ) : null}
        {!editorCap.available ? (
          <UnavailableTool label="编辑器" reason={editorCap.reason ?? '编辑器尚未开放'} />
        ) : null}
        {!browserCap.available ? (
          <UnavailableTool label="浏览器" reason={browserCap.reason ?? '浏览器尚未开放'} />
        ) : null}
        <Button
          variant="ghost"
          disabled
          aria-label="删除工作空间"
          title={delCap.reason ?? '删除工作空间尚未开放'}
        >
          删除工作空间
        </Button>
      </details>
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
