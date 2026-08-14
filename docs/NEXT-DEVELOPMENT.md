# Cockpit Next development runtime

Cockpit Next is developed from `${HOME}/github/agent-cockpit-next` while the
deployed `0.3.4` service remains frozen on `127.0.0.1:8790`.
The reviewed 2.0 tree is currently a local `next` preview: at this checkpoint,
the reviewed 2.0 exact has not been published to `origin/next`, so a public
clone does not contain it yet.

## N0 boundary

- Branch: `next`, based on `169d0af7751b568e813d2cbca285a9f147e86001`.
- Project key and Agent Mail project: `agent-cockpit-next` and the exact Next
  worktree path.
- Reserved Herdr session: `github-agent-cockpit-next`.
- Web endpoint: `127.0.0.1:18790` by default; fixed Next may instead bind
  `0.0.0.0:18790` only with a valid private token file.
- Runtime roots: `~/.local/share/agent-cockpit-next-data`,
  `~/.config/agent-cockpit-next`, `~/.local/state/agent-cockpit-next`, and
  `~/.local/share/agent-cockpit-next-uploads`.
- Project discovery root: the non-secret `COCKPIT_PROJECT_ROOT` value in
  `.env.next` (the example uses `${HOME}/github`). Fixed Next exposes only this
  root in the Project wizard; file browsing keeps its existing root groups.
- Development uses a source process. It does not install a systemd unit. A
  future isolated unit may only be named `agent-cockpit-next.service`.
- Upgrade V2, B0, and Herdr socket state are disabled in the N0 profile.
- Source `/health/live` reports `source_sha=unknown`; the Gate separately proves
  that the `next` branch contains the exact `169d0af...` baseline.

The Gate rejects missing or extra environment keys, port `8790`, the production
unit name, production runtime roots, the old Agent Mail scope, the old Herdr
session, symlinked runtime roots, and a worktree or Git baseline mismatch.
It also removes inherited Cockpit, Herdr, XDG, Hub, Python import, virtualenv,
and dynamic linker variables before starting the source interpreter. XDG roots,
the local Hub endpoints, the Herdr config/session root, and the shared read-only
Agent Mail database path are then set explicitly; server-side queries and writes
remain restricted to the exact Next project. The fixed SHA in the Gate
intentionally cross-checks the independently versioned delivery baseline.

The only optional secret input is
`~/.config/agent-cockpit-next/cockpit.token`. When present, it must be a
regular file owned by the current user, have mode `0600`, have exactly one hard
link, and contain one 32-256 character ASCII token using letters, digits, `_`,
or `-`. The launcher reads it through a no-follow descriptor after isolation
validation and injects only `COCKPIT_TOKEN` into the server process. A missing
file preserves local-only mode and requires `COCKPIT_HOST=127.0.0.1`. Setting
`COCKPIT_HOST=0.0.0.0` requires this file; the launcher and server both fail
closed unless the injected token exactly matches it. No other host, IP, or
hostname is accepted. Do not add the token to `.env.next`, the shell
environment, Git, logs, or test reports. Ephemeral test servers always remain
pinned to `127.0.0.1`.

## Commands

The fixed profile requires Git, Python 3.12+, and Node.js 20+ with npm. An
operator must first synchronize a local `next` checkout/worktree at the fixed
path. From that existing checkout, create independent Python and Web
dependencies, build the production Web app, and validate the profile:

```bash
cd "$HOME/github/agent-cockpit-next"
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm ci --prefix web
npm run --prefix web build
cp .env.next.example .env.next
# Edit COCKPIT_PROJECT_ROOT to the concrete directory containing your Git repositories.
.venv/bin/python scripts/next_dev.py check --env-file .env.next
.venv/bin/python -m pytest -q tests/test_next_isolation.py
```

`COCKPIT_PROJECT_ROOT` must be an existing canonical directory narrower than
the whole Home directory. Do not set it to `/`, `/home`, or `${HOME}`. The
fixed Next profile requires the Cockpit checkout at
`${HOME}/github/agent-cockpit-next`; only the container holding user repositories
is configurable, for example `/mnt/data/projects`. The browser receives only an
opaque `root_id` and a relative locator; it never submits this absolute path.
Missing or unsafe configuration fails the launcher check with a stable
`project_root_*` error instead of falling back to Home or `/`.
Both `check` and `start` also require `web/dist/index.html` and built assets;
missing output fails closed with `next_web_build_unavailable`. Run
`npm run --prefix web build` again after changing the Web app.

Start the source development service only after the checks pass:

```bash
.venv/bin/python scripts/next_dev.py start --env-file .env.next
```

## First use

1. Open `http://127.0.0.1:18790`. If the instance is exposed on a private LAN
   and shows a login page, enter the access token provided by the operator. Do
   not put that token in chat, logs, or project files.
2. On the empty overview, choose **选择代码目录**. Cockpit skips location choices
   when there is only one usable local node and one configured code root.
3. Choose the directory that directly contains the repository's `.git`
   directory, then choose **检查并继续** and **确认添加**. The configured code
   root is only a container and is not itself a project.
4. Choose **继续创建工作空间**. Keep the default `main` name or enter a clearer
   name, then choose **创建并打开**.
5. The new workspace opens at **文件**. Select a file to inspect it, then use
   **打开终端** and **新终端** to run commands in that workspace.

After this path works, returning users can choose a project from the project
switcher, open an existing workspace, and continue from Files or Terminal.

## Public release blocker

The reviewed 2.0 exact has not been published to the public `origin/next`
branch. Do not present a public clone as a current installation path. Only
after the reviewed `next` branch is published and the remote is verified to
contain that exact may a new user run:

```bash
mkdir -p "$HOME/github"
git clone --branch next https://github.com/fyc0451/agent-cockpit.git \
  "$HOME/github/agent-cockpit-next"
```

To enable token authentication, create the private file before `check` and
`start` (the no-clobber setting refuses to overwrite an existing token):

```bash
install -d -m 700 "$HOME/.config/agent-cockpit-next"
(umask 077; set -o noclobber; openssl rand -hex 32 > \
  "$HOME/.config/agent-cockpit-next/cockpit.token")
```

Changing this file and restarting rotates the authentication state. Use HTTPS,
an SSH tunnel, Tailscale, or another authenticated private transport for
non-loopback clients. Direct `0.0.0.0` binding serves plain HTTP: another party
on the LAN can observe and replay the reusable login cookie. Prefer an HTTPS
reverse proxy or Tailscale, restrict the trusted network, and never publish the
endpoint to the public Internet. To opt into direct LAN binding after creating
the token, set `COCKPIT_HOST=0.0.0.0` in `.env.next`; changing either the host or
token requires a restart.

This `start` command is the only supported entry point for the Next backend. It
hands a per-profile process lock to the server across `execve`; direct Next
server invocation without that validated launcher handoff fails closed. The N0
profile continues to reject symlinked runtime roots. Internally, canonical
identity resolution maps aliases of the same roots to one identity, while
profiles with different real roots may run concurrently. Lock metadata is only
diagnostic and is never used to signal or terminate a process. Server handoff
requires both a conflicting independent lock probe and successful nonblocking
lock reassertion on the inherited descriptor; its lifespan refuses to start
without the adopted owner.

The service is expected at `http://127.0.0.1:18790` by default, or at the
machine's private LAN address on port `18790` after the explicit `0.0.0.0`
opt-in. Do not run `install.sh`,
`upgrade.sh`, `launchd.sh`, or any command targeting `agent-cockpit.service`
from this worktree.
