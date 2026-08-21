# Changelog

All notable changes to this project will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Cockpit 4.0 first slice on `feature/cockpit-4.0-team-zone`:
  isolated team ledger `team-messages.json`. Team Hub history
  never writes `chat-messages.json`. Team send / receive /
  hand-to-leader stay on `/api/team/ledger*` and never call
  `chat_ledger.append_message` or `pane_send`. Selecting a
  team topic opens the team timeline (send one, see one);
  local waterfall and composer stay unused. Hand-to-leader is
  a team-timeline action (optional default on send) and never
  copies into the local waterfall. Sidebar topics come from
  Hub `/api/team/projects`; bind conflict 409 can replace after
  confirm.

### Changed

- `install.sh` now installs Cockpit 3.0: it builds `web/dist` and
  starts `scripts/dev_server.py` via systemd / LaunchAgent. The old
  board is no longer the one-command result.

- Source 8790 no longer requires `$HOME/github`. The discovery root
  is the checkout parent (or the checkout itself), overridable with
  `COCKPIT_PROJECT_ROOT`.

- Group-chat send defaults to queue. Interrupt stays available for
  the cases that need to stop current work.

### Added

- Group chat warns when a pane cannot send Agent Mail, so a
  disconnected agent is not mistaken for a silent harvest.

### Fixed

- Harvest no longer pastes the next conclusion into the previous
  bubble when the terminal still shows the old one. A later 结论
  heading is a new message, not a longer copy of the last.

- Harvest reads the current Claude/Grok screen when `recent-unwrapped`
  is an empty prompt box or leftover scrollback. `检查结果` /
  `原因分析` count as conclusion headings.

- Queued group-chat mail waits until the pane has been idle for a
  couple of seconds, and skips a queue prompt when that pane already
  answered later in the terminal.

- Harvest recognizes Kimi/TUI `● 结论` / `• 结论` headings. A later
  conclusion is scraped on its own; the previous turn in scrollback
  is not treated as a longer copy of the last bubble.

- Harvest drops Claude/Kimi Write/Update file dumps, keeps the last
  conclusion (`结论` / `实现完成总结` / `总结`), and does not stop the
  bubble at a mid-screen `❯` prompt. The live scrape window is 240
  lines so a long reply is not cut at a numbered dump line.

- Harvest no longer leads a bubble with Codex git cards, Read/Search
  dumps, or JSON fragments. The visible head is the reply, not the
  tool transcript still on screen.

## [0.3.7] - 2026-08-20

### Removed

- Drop unmounted 2.0 pages (Overview / Projects / Inbox / Files /
  Terminal / workbench) from the 3.0 web tree. HashRouter still only
  serves `/chat` and `/settings`; leftover deep links land in group chat.

### Added

- Settings now has a one-click upgrade tab for the source 8790
  checkout. It pulls the official GitHub tag, rebuilds `web/dist`, and
  restarts `agent-cockpit-source-8790`. Native V2 stays off so this
  process is not replaced by the packaged unit.

### Fixed

- Opening group chat now lands on the latest bubble. The waterfall pins
  after layout and keeps following while the user is already at the
  bottom, so later height (folded replies, fonts) does not leave the
  view at the top.

### Changed

- Publish version 0.3.7 as the signed operator package for the source
  8790 checkout already serving that upgrade tab and waterfall pin.

## [0.3.6] - 2026-08-20

### Changed

- Keep group-chat harvest on idle/done only. Do not `agent read` a
  working pane. Store the cleaned full reply in the ledger, and let the
  waterfall show the conclusion first with process folded away.

- Rename Herdr `blocked` in the group chat to 等你输入: live bubble,
  member status, busy-strip, activity line, and interact modal. The
  underlying status stays `blocked`.

- Push ledger append/replace/receipt over
  `/api/chat/sessions/{name}/mail/stream`. The waterfall follows that
  stream and stops the 2s empty poll while the socket is live.

- Show workspace git on the Files tab: current branch, dirty file
  count, expandable stat and diff. Harvest no longer attaches the
  whole-tree status to a reply bubble.

- Publish version 0.3.6 as the signed operator package for the Cockpit
  3.0 group chat already served on :8790. A workspace is a directory,
  each session is a 1:1 Herdr binding, Agent Mail stays durable, and
  Herdr is runtime only. Managed worktree lifecycle governance remains
  follow-up work.
- Record version 0.3.5 as the earlier source-tree operator preview of
  that same group-chat reconstruction.

- Publish version 0.3.4 with the versioned delivery framework and the native
  consecutive-upgrade evidence fix.
- Publish version 0.3.3 without runtime or frontend changes so the signed native
  release and GUI upgrade flow can be exercised end to end by the operator.

### Fixed

- Detect ledger SSE inserts by message id, not window length, and keep
  a 10s waterfall poll as backup so a live socket cannot hide new
  bubbles until refresh.

- Skip leftover Codex/Claude identity inject when a pane hook has no
  next-profile environment, and treat leftover names (`codex-luna`,
  `codex-main`, `*-agent-*`) as no identity so `mail-hook-check` exits 0
  instead of looping identity chrome into the TUI.
- Keep diagnosis lines that mention leftover keywords, and still drop
  wrap leftover inject so the waterfall does not hide a short Chinese
  diagnosis or replay hook chrome.
- Treat already-stopped or missing Herdr sessions as a successful delete,
  and clear a deleted session from the URL so the page does not refresh in
  a loop.
- Skip persist-work wakes when every remaining handoff item is watch/wait,
  and allow identity retirement to finish when the project directory is
  already gone.
- Publish draft GitHub Releases through their validated numeric API endpoint so
  the local signed release lane can complete without manual reconciliation.
- Rebind previous-generation evidence after a completed maintenance request so a
  later native upgrade can start from the newly active generation.
- Accept pane-scoped `typing.json` in sealed upgrade snapshots so a valid
  terminal input-avoidance state cannot block native upgrades.
- Set OpenCode's light/dark mode explicitly in addition to selecting its theme,
  so the light Web theme renders a genuinely light OpenCode background.
- Switch live OpenCode themes through its dedicated theme dialog so rapid
  changes cannot leave commands in the composer or submit them as chat.
- Block terminal pointer and keyboard input while a terminal is loading or its
  theme is repainting, preventing delayed clicks from replaying after the UI
  becomes responsive while still allowing required xterm protocol replies.
- Confirm Herdr pane-set changes against an authoritative snapshot before
  rebuilding parameterized subscriptions, preventing stale pane events from
  causing a permanent `pane_not_found` reconnect loop.
- Messages Session control is navigation-only (jump to mail-project human_key),
  with clearer chips/toasts; message body still loads from the project library.
- Layout「拆开整组」resolves the multi-pane group from a fresh snapshot
  `sessions[].focused_pane_id` instead of a stale BOARD with multiple focused panes.
- Layout quick actions now compose two existing Agent panes and never create or
  close panes; empty-shell splits live under a confirmed advanced control.
- Theme switching is in-place and latest-request-wins across Web, Herdr, Grok
  (`/theme light|dark`), and OpenCode (`/themes` with `palenight`/`aura`), while
  preserving existing OpenCode TUI settings and refusing to overwrite invalid JSON.


### Added

- Durable chat ledger for workspaces, threads, and waterfall messages, plus
  an in-process persist-work harness that wakes an idle Leader when handoff
  still has real next steps and skips watch-only or "wait for Boss" items.
- Group-chat web shell with 2–4 pane compose, overlay login that keeps
  composer drafts, terminal font controls, and harvest text that strips TUI
  chrome.
- A strict versioned delivery contract and read-only gate with stable validation
  codes, dependency readiness reporting, release checks, and adversarial fixtures.
- A compact historical task-statistics strip in the workbench, backed by an
  unbounded read-only aggregate for status, completion, and duration percentiles.
- A read-only project workbench API that combines durable coordination
  assignments with exactly bound, field-restricted Herdr session summaries and
  degrades safely when live Herdr state is unavailable.
- Authenticated HTTP endpoints for creating, listing, reading, transitioning,
  and closing project-scoped coordination assignments with optimistic CAS.
- A managed release-lane CLI that serializes local publishers, rejects stale
  `origin/main` baselines before mutation, keeps its lock through child-process
  failure, and writes credential-free atomic release receipts.
- A dormant durable delivery-outbox store with atomic claiming, idempotent
  enqueue, retry/dead-letter transitions, fail-closed legacy migration, and
  credential-safe persistence. It is not yet wired into the server or Hub worker.
- Persistent coordination assignments with durable create/list operations,
  explicit status transitions and close, stable ordering, and optimistic CAS
  versions for concurrent agents.
- Full-width Messages workspace with top filters for project, session, sender,
  recipient, time range, and status, including exact session-to-project mapping.
- Localized board degradation banners and a dedicated unavailable-agent column,
  with strict Herdr-binary detection so recoverable socket/cache failures show a
  retry path instead of misleading installation guidance.
- Leader-binding ledger and deferred-delivery runtime wiring for Team Inbox
  routing, with versioned CAS migrations, a replayable outbox, deduplication,
  retries, binding-generation handoff, and explicit `off`/`shadow`/scoped
  `canary`/`on` rollout modes. The runtime remains `off` by default and does not
  open the binding store, install claim gates, or poll until explicitly enabled.
- Herdr native lifecycle control for all supported agents: capability and identity
  gates, managed `agent start`/prompt/wait primitives, identity-preserving restart,
  and a default-off event-driven socket state cache with an explicit session-scoped
  canary mode.
- Machine-verifiable `/health/live` release identity and a side-effect-free
  `/health/ready` compatibility report for runtime paths, persisted stores, and
  release artifacts.
- Team collaboration design docs: overall personal/team dual-mode design and the
  project channel technical design (方案 Y), plus the M1a deployment guide for a
  shared hub reachable on the intranet.
- `hub_client` now honors the `hub` field in `~/.agent-mail/client.env` via the shared
  `am_common.load_client_config` parser, so cockpit write operations can target a
  shared team hub while personal mode (`127.0.0.1:8765`) stays the default.
  **Upgrade note:** this removes the previous force-`localhost` override — before
  upgrading, review the `hub=` value in `~/.agent-mail/client.env` (a remote or empty
  value now takes effect); restart cockpit after editing it.
- Bundled Agent Mail helper commands, including Claude registration, with safe
  `~/.local/bin` links during install and upgrade.
- Python 3.14 coverage in the Linux and macOS CI matrix.

### Fixed

- Validate assignment transition legality inside the same database transaction
  as its versioned update, preventing concurrent writers from committing an
  invalid state transition.
- Restore the full-width flow after leaving a mobile terminal and split cramped
  mobile terminal panes into independent tabs before attaching.
- Preserve and safely recover eligible pending background tasks after a Cockpit
  restart, while failing interrupted running tasks closed and rejecting dirty or
  mismatched worktrees before relaunch.
- Keep the event-driven Herdr state cache current by subscribing to each pane's
  agent-status events and rebuilding subscriptions when the pane set changes,
  without adding CLI polling or expanding the configured canary scope.
- Accept JSON and SSE FastMCP inbox responses, including structured tool results,
  and poll only unread messages so enabling B0 cannot replay existing read history.
- Retire the legacy in-process Git/pip upgrade executor and fail closed instead of
  allowing the application to mutate its own live checkout.
- Reject unsafe runtime path overrides, symlink escapes, colliding stores, and
  persisted file roots before they can authorize reads or writes outside the
  configured profile.
- Persist one canonical Agent Mail human key per Herdr session, unify linked Git
  worktrees under their main worktree, and require explicit selection for ambiguous
  legacy sessions instead of guessing from pane cwd.
- Stop fabricating `<agent>-main` identities and route pane notifications through the
  persisted session binding while preserving Agent Mail as an optional dependency.
- Serialize complete Web PTY input messages per terminal so reconnecting or concurrent
  WebSockets cannot interleave bytes, and make the large-input regression wait for an
  explicit foreground receiver-ready signal instead of depending on login-shell job
  control timing.
- Serve the pinned xterm.js browser assets locally so terminal page loads no longer
  wait on a slow or unavailable third-party CDN.

### Security

- Upgrade `python-multipart` to 0.0.31 and pytest to 9.0.3 to include current upstream
  security fixes.

## [0.1.0] - 2026-08-03

### Added

- Web cockpit for Herdr panes, Agent Mail, files, uploads, terminals, and Codex tasks.
- Mobile Herdr flow view with scroll-preserving refresh and pane input.
- Shared-token authentication for remote access and a loopback-only safe default.
- Install, upgrade, uninstall, and environment-diagnostic scripts.
- Unified Attention Inbox for blocked panes, failed tasks, pending diffs, and unread messages.
- Opt-in Web Push notifications with pane/task/message deep links.
- Root-scope Web App manifest and iPhone Home Screen guidance for mobile push.
- Trilingual UI (中文/English/日本語) with dark/light theme; light mode auto-inverts explicit dark ANSI colors painted by TUIs.
- Settings page: per-directory default agent, enabled agents, runtime limits, terminal font size, and an environment self-check (`/api/env-check`).
- First-run onboarding: board empty-state guide straight into the one-click workspace.
- Trilingual READMEs (中文 default, English, 日本語) with screenshots.

### Changed

- Agent Mail is optional; missing databases hide messaging, while Hub outages leave messages read-only.

### Security

- Sandboxed file roots and isolated Git worktrees for background tasks.
- Same-origin checks, WebSocket authentication, output escaping, and input validation.
