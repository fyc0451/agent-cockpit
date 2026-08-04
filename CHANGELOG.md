# Changelog

All notable changes to this project will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Bundled Agent Mail helper commands, including Claude registration, with safe
  `~/.local/bin` links during install and upgrade.
- Python 3.14 coverage in the Linux and macOS CI matrix.

### Fixed

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
