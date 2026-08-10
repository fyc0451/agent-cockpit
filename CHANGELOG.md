# Changelog

All notable changes to this project will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Confirm Herdr pane-set changes against an authoritative snapshot before
  rebuilding parameterized subscriptions, preventing stale pane events from
  causing a permanent `pane_not_found` reconnect loop.
- Messages Session control is navigation-only (jump to mail-project human_key),
  with clearer chips/toasts; message body still loads from the project library.
- Layout「拆开整组」resolves the multi-pane group from a fresh snapshot
  `sessions[].focused_pane_id` instead of a stale BOARD with multiple focused panes.
- Theme switch writes preference and force-reloads the page to avoid broken
  xterm/WebGL runtime theme state.


### Added

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
