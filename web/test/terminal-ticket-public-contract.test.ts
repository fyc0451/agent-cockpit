import { ProtocolError } from '../api/client'
import {
  assertTerminalTicketView,
  type TerminalRuntime,
  type TerminalTicket,
} from '../api/terminals'

type ForbiddenPublicKey = Extract<
  keyof TerminalTicket | keyof TerminalRuntime,
  'herdr_session' | 'pane_id'
>

const noForbiddenPublicKeys: [ForbiddenPublicKey] extends [never] ? true : false = true

const ticket = {
  ticket_id: 'ttk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  project_id: 'prj_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  workspace_id: 'ws_cccccccccccccccccccccccccccccccc',
  desired_state: 'running',
  observed_state: 'running',
  engine_generation: 1,
  reconnect_cursor: 0,
  receipt_refs: [],
  revision: 1,
  created_at: '2026-08-18T00:00:00.000000Z',
  updated_at: '2026-08-18T00:00:00.000000Z',
}

const runtime = {
  state: 'running',
  replay_available: true,
  replay_truncated: false,
}

describe('terminal ticket public contract', () => {
  it('公开类型不声明 Herdr session/pane 字段', () => {
    expect(noForbiddenPublicKeys).toBe(true)
  })

  it.each(['herdr_session', 'pane_id'] as const)(
    'fail-closed 拒绝 ticket 或 runtime 额外字段 %s',
    (field) => {
      expect(() => assertTerminalTicketView({
        ticket: { ...ticket, [field]: 'forbidden' },
        runtime,
      })).toThrow(ProtocolError)
      expect(() => assertTerminalTicketView({
        ticket,
        runtime: { ...runtime, [field]: 'forbidden' },
      })).toThrow(ProtocolError)
    },
  )
})
