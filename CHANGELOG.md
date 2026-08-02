# Changelog

All notable changes to this project will be documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Web cockpit for Herdr panes, Agent Mail, files, uploads, terminals, and Codex tasks.
- Mobile Herdr flow view with scroll-preserving refresh and pane input.
- Shared-token authentication for remote access and a loopback-only safe default.
- Install, upgrade, uninstall, and environment-diagnostic scripts.
- Unified Attention Inbox for blocked panes, failed tasks, pending diffs, and unread messages.
- Opt-in Web Push notifications with pane/task/message deep links.

### Changed

- Agent Mail is optional; missing databases hide messaging, while Hub outages leave messages read-only.

### Security

- Sandboxed file roots and isolated Git worktrees for background tasks.
- Same-origin checks, WebSocket authentication, output escaping, and input validation.
