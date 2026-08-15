import { newIdempotencyKey } from '../api/idempotency'

export interface WorkDraftFields {
  body: string
  acceptance: string
  constraints: string
}

export interface WorkDraft extends WorkDraftFields {
  intentKey: string
}

export type WorkDraftField = keyof WorkDraftFields

const DRAFT_PREFIX = 'cockpit.workDraft.v1'
const LAST_WORKSPACE_KEY = 'cockpit.lastWorkspace.v1'

function storageKey(projectId: string, workspaceId: string): string {
  return `${DRAFT_PREFIX}:${encodeURIComponent(projectId)}:${encodeURIComponent(workspaceId)}`
}

export function emptyWorkDraft(): WorkDraft {
  return { body: '', acceptance: '', constraints: '', intentKey: newIdempotencyKey() }
}

export function loadWorkDraft(projectId: string, workspaceId: string): WorkDraft {
  try {
    const raw = window.localStorage.getItem(storageKey(projectId, workspaceId))
    if (raw === null) return emptyWorkDraft()
    const value = JSON.parse(raw) as Partial<WorkDraft>
    if (
      typeof value.body !== 'string' ||
      typeof value.acceptance !== 'string' ||
      typeof value.constraints !== 'string' ||
      typeof value.intentKey !== 'string' ||
      value.intentKey === ''
    ) {
      return emptyWorkDraft()
    }
    return value as WorkDraft
  } catch {
    return emptyWorkDraft()
  }
}

export function updateWorkDraft(
  projectId: string,
  workspaceId: string,
  draft: WorkDraft,
  field: WorkDraftField,
  value: string,
): WorkDraft {
  if (draft[field] === value) return draft
  const next = { ...draft, [field]: value, intentKey: newIdempotencyKey() }
  try {
    window.localStorage.setItem(storageKey(projectId, workspaceId), JSON.stringify(next))
  } catch {
    // The in-memory draft remains usable when browser storage is unavailable.
  }
  return next
}

export function clearWorkDraft(projectId: string, workspaceId: string): void {
  try {
    window.localStorage.removeItem(storageKey(projectId, workspaceId))
  } catch {
    // Saving succeeded remotely; an unavailable storage backend needs no local cleanup.
  }
}

export function hasDraftContent(draft: WorkDraft): boolean {
  return draft.body !== '' || draft.acceptance !== '' || draft.constraints !== ''
}

function readLastWorkspaces(): Record<string, string> {
  try {
    const value = JSON.parse(window.localStorage.getItem(LAST_WORKSPACE_KEY) ?? '{}') as unknown
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return {}
    return Object.fromEntries(
      Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    )
  } catch {
    return {}
  }
}

export function rememberLastWorkspace(projectId: string, workspaceId: string): void {
  try {
    window.localStorage.setItem(
      LAST_WORKSPACE_KEY,
      JSON.stringify({ ...readLastWorkspaces(), [projectId]: workspaceId }),
    )
  } catch {
    // Workspace navigation still works without persistence.
  }
}

export function loadLastWorkspace(projectId: string): string | null {
  return readLastWorkspaces()[projectId] ?? null
}
