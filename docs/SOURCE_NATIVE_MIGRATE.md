# One-shot source → native migration (B0)

**Audience:** release manager only.

**Not for GUI:** the Cockpit upgrade button never invokes this path. After migration,
GUI/controller upgrades are native → native only.

## Safety defaults

| Mode | Mutates | Services |
| --- | --- | --- |
| `plan` | no | no |
| `preflight` | no | validates inputs only |
| `execute` | yes | only with **both** `--confirm-source-native-migration` and `--allow-live-service-ops` |

Never generate keys, tags, or GitHub Releases here. Provide an already-verified signed
release index + detached signature + long-term public key (32-byte raw).

## Layout (single frozen native tree)

Native paths come **only** from `upgrade_layout.default_upgrade_layout(home=…)` —
the same contract as GUI / `upgrade_service`. There is **no** second native layout.

| Role | Path |
| --- | --- |
| Native `deploy_root` | `~/.local/share/agent-cockpit-server` (`layout.deploy_root`) |
| `current` / `generations` / `helpers` | under that `deploy_root` |
| `controller_root` + install public key | `layout.controller_root` (`~/.local/share/agent-cockpit-controller`) |
| Source deployment / rollback tree | `~/.agent-cockpit-deployments/…` only (not native root) |
| Release-external host config | `~/.config/agent-cockpit/server.env` |

`--deploy-root`, if passed, must **exact-equal** `layout.deploy_root`. Passing
`~/.agent-cockpit-deployments` as native root is rejected (`deploy_root_mismatch`).

## Environment contract (production-critical)

Production source units often declare `EnvironmentFile=-…/.env` **while the file is
absent**, and keep runtime knobs on unit `Environment=` lines
(`COCKPIT_EDITION`, source SHA, upgrade gate, Herdr canary, B0, …).

Migration **never** requires source `.env` to exist, and **never** scrapes unit
`Environment=` lines into a file (no guessing). After the native unit switch, runtime
config must come from release-external `server.env` only.

### Path A — source `.env` present

1. Validate regular file, owner=current user, mode `0600`.
2. Atomically copy **exact bytes** to `--persistent-env` (`server.env`, `0600`).
3. If `server.env` already exists with the **same** bytes → reuse (idempotent).
4. If both exist with **different** bytes → fail (`env_content_mismatch`).

### Path B — source `.env` absent (typical production)

Release manager must **pre-provision** `server.env` **before** plan/preflight/execute:

```bash
install -d -m 700 "$HOME/.config/agent-cockpit"
# Author the real runtime keys yourself (edition, upgrade gate, Herdr canary, B0, …).
# Migration will NOT invent this file from unit Environment= lines.
umask 077
cat > "$HOME/.config/agent-cockpit/server.env" <<'EOF'
# example keys only — use production values
COCKPIT_EDITION=source
COCKPIT_UPGRADE_V2_ENABLED=0
COCKPIT_HERDR_STATE_MODE=canary
COCKPIT_HERDR_STATE_CANARY_SESSIONS=github-agent-cockpit
COCKPIT_B0_MODE=on
EOF
chmod 600 "$HOME/.config/agent-cockpit/server.env"
# owner must be the service user; regular file (not symlink)
```

Migration then **reuses** that file (validates owner/mode/release-external path) and
does not rewrite or log its contents.

### Fail closed

| Condition | Code |
| --- | --- |
| Neither source `.env` nor `server.env` | `env_unavailable` |
| Both present, different bytes | `env_content_mismatch` |
| Present file wrong owner/mode/symlink | `source_env_*` / `persistent_env_*` |

`plan` / `preflight` may print **paths** and strategy notes; they never print env values.

## Inputs

- `--release-tag` — human tag/id for diagnostics (e.g. `agent-cockpit-v0.3.0`)
- `--public-key` — path to 32-byte Ed25519 public key used to verify the index
- `--index` / `--signature` — verified release index JSON + signature bytes
- `--deploy-root` — optional; default / only legal value is frozen
  `~/.local/share/agent-cockpit-server`
- `--source-unit` — current source `agent-cockpit.service` path
- `--source-env` — candidate source `.env` path (may be missing)
- `--persistent-env` — release-external **`server.env`** (required destination / pre-provision path)
- `--evidence-env` — optional release-external `server-evidence.env` selector
- `--diagnostics-dir` — backup unit + failure.json (kept on rollback)
- `--expected-source-sha` — optional exact SHA gate for prepared generation
- `--home` — optional override for `default_upgrade_layout` roots (tests)

## Contract

1. **Before maintenance:** `prepare_generation` into frozen native `deploy_root` +
   install controller under `layout.controller_root` (same layout object).
2. **Save** source unit original bytes under diagnostics.
3. **Stop** only `agent-cockpit.service` then `agent-mail.service` — never Herdr/agents.
4. **Activate** `layout.current` → new generation (half-complete retry: if `current` already
   points at the prepared target, re-activate is idempotent); install
   `helpers/*` → `../current/bin/agent-cockpit`.
5. **Env path A or B** (above) so native stack keeps release-external `server.env`.
6. Write fixed native unit (`KillMode=process`, WD=`…/current`,
   ExecStart=`…/current/bin/agent-cockpit serve`,
   `EnvironmentFile=-…/server.env` only — **not** `current/.env`, and **not** a copy of
   old unit `Environment=` knobs).
7. **Start** `agent-mail.service` then `agent-cockpit.service`; verify `/health/live`
   `identity.source_sha` exact.
8. **Any failure after partial stop or unit switch:**
   - best-effort `failure.json` only (write failure never skips rollback);
   - **stop Cockpit then Mail first** (native stack may already be *active* after a
     post-native health mismatch; `start` alone does not restart active units);
   - restore pre-migration `current` exact symlink text or absence;
   - atomic same-dir replace of source unit original bytes;
   - `daemon-reload`; start Mail then Cockpit; verify old `/health/live` matches
     pre-stop SHA;
   - restore failure → stable `rollback_failed` (primary exception preserved as cause);
   - generation/diagnostics kept; source tree / source `.env` (if any) intact.
9. **Half-complete retry:** default prepare/controller reuses exact existing
   generation/controller only after strict launcher/key consistency checks;
   inconsistency fail-closed (`generation_inconsistent` / `controller_inconsistent`).
10. Unit install and unit rollback always use same-dir temp + fsync + `os.replace`
    (never truncate-in-place).

## Example (dry)

```bash
python3 source_native_migrate.py plan \
  --release-tag agent-cockpit-v0.3.0 \
  --public-key /path/to/release.pub \
  --index /path/to/index.json \
  --signature /path/to/index.sig \
  --deploy-root "$HOME/.local/share/agent-cockpit-server" \
  --source-unit "$HOME/.config/systemd/user/agent-cockpit.service" \
  --source-env "$HOME/.agent-cockpit-deployments/fullscreen-<id>/.env" \
  --persistent-env "$HOME/.config/agent-cockpit/server.env" \
  --diagnostics-dir "$HOME/.local/state/agent-cockpit/migrate-b0" \
  --expected-source-sha <40-char-sha>
```

## Example (execute — production opt-in)

```bash
# Path B first when source .env is missing (see pre-provision above).
python3 source_native_migrate.py execute \
  --confirm-source-native-migration \
  --allow-live-service-ops \
  --release-tag agent-cockpit-v0.3.0 \
  --public-key /path/to/release.pub \
  --index /path/to/index.json \
  --signature /path/to/index.sig \
  --deploy-root "$HOME/.local/share/agent-cockpit-server" \
  --source-unit "$HOME/.config/systemd/user/agent-cockpit.service" \
  --source-env "$HOME/.agent-cockpit-deployments/fullscreen-<id>/.env" \
  --persistent-env "$HOME/.config/agent-cockpit/server.env" \
  --diagnostics-dir "$HOME/.local/state/agent-cockpit/migrate-b0" \
  --expected-source-sha <40-char-sha>
```

Prefer wrapping under `release_lane.py` when changing `origin/main` or shared deploy state.
