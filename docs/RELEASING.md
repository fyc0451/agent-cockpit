# Managed Releases

`upgrade.sh` is the consumer-side, pre-stable source deployment updater. It only
fast-forwards an installed checkout to its current upstream, delegates dependency,
build, and supervisor work to `install.sh`, verifies `/health/live`, and rolls back
the checkout and install on failure.

Publishing a source release is a separate managed operation and must run through
`release_lane.py`; direct `git push ... main` remains outside the supported release
path. The legacy V1 Web upgrade API and worker remain retired.

## Release lane v1

Prepare one executable command or script containing the complete mutation window:
push the fixed candidate, create its immutable deployment checkout and rollback
unit, switch the service, and run the release-specific health checks. Then run it
as one child process:

```bash
python3 release_lane.py run \
  --repo /absolute/path/to/clean-release-worktree \
  --expected-main <40-character-origin-main-sha> \
  --candidate <40-character-descendant-sha> \
  --release-id <unique-non-secret-id> \
  -- /absolute/path/to/release-command
```

The lane:

- takes a non-blocking, per-user `flock` before Git or release mutations;
- rejects a second publisher immediately;
- reads `origin/main` directly and rejects a stale expected SHA;
- requires the clean worktree HEAD and candidate to descend from that SHA;
- keeps the lock descriptor in the child, so killing only the guard cannot expose
  an in-flight release to a second publisher;
- requires `origin/main` to equal the candidate after a successful child command;
- atomically writes a mode-0600 JSON receipt with the rollback SHA and result.

The default state directory is
`$XDG_STATE_HOME/agent-cockpit/release-lane`, falling back to
`~/.local/state/agent-cockpit/release-lane`. Set
`AGENT_COCKPIT_RELEASE_STATE_DIR` only to an absolute, user-owned directory on a
local filesystem.

Receipts deliberately omit the child command, environment, stdout, and stderr.
Do not put credentials in the release id or command line; load them inside the
managed command from the existing protected environment when required.

## Failure handling

A rejected receipt means no child command ran. A failed receipt can represent a
partial release; compare `observed_main_after`, `candidate`, and `rollback_sha`
before taking action. Never reuse a release id. The lane serializes publishers but
does not infer application-specific rollback steps or make an unsafe automatic
rollback decision.

This first version is an enforced team workflow, not an OS security boundary:
someone can still bypass it by running raw commands. Repository contributors and
agents must treat such bypasses as unsupported release failures.

## Local signed native releases

GitHub-hosted workflows are manual-only. Normal pushes, pull requests, and tag
creation do not consume Actions minutes. A release manager can publish from the
Linux release host instead:

1. Provision a repository-scoped GitHub token and long-term Ed25519 key once. The
   token and private key stay outside the repository as mode-0600 files:

   ```bash
   gh config get oauth_token --host github.com \
     | .venv/bin/python scripts/provision_local_release.py --github-token-stdin
   ```

2. Run the full local test/build gate, then publish an exact clean commit already
   present at `origin/main`:

   ```bash
   .venv/bin/pip install -r requirements-build.txt
   .venv/bin/python scripts/publish_local_release.py \
     --candidate <40-character-origin-main-sha> \
     --release-id <unique-non-secret-id>
   ```

The publisher builds the native archive, signs the canonical index, verifies the
archive locally, creates a draft Release, downloads all three assets back from
GitHub, verifies them again, and only then publishes. Evidence is retained under
`~/.local/state/agent-cockpit/local-releases/<release-id>/`.

Private repositories use `~/.config/agent-cockpit/github-release.token` at
runtime. The credential is sent only to GitHub API endpoints and is removed before
following an allowlisted release-CDN redirect. The matching public key is installed
into the native controller during the one-time source-to-native migration.
