# Next Ephemeral Harness

`scripts/next_ephemeral_server.py --runtime-root <directory>` starts one real,
local Cockpit Next process for ordinary live-journey tests.

## Contract

- The caller creates `<directory>` as an owned, completely empty `0700` directory. The
  first launch binds it to a private marker before creating its fixed child layout, then
  atomically marks it `running` before `exec`. Only a real clean stop writes `ready` and
  a recursive catalog; a same-root restart accepts only that exact
  marker/catalog and layout, including each path's type, mode, owner, single-link regular
  file status, and content digest. The sole literal exception is `data/instance.lock`:
  its contents are rewritten by the acquired lock itself, while its path, type, mode,
  owner, and link count remain exact and `InstanceLock` validates its live metadata.
  Missing, extra, changed, linked, or non-catalogued entries fail closed. The launcher
  leaves cleanup to the caller after the process has been reaped.
- The launcher uses `COCKPIT_NEXT_PROFILE=ephemeral`, a unique `HERDR_SESSION`, private
  runtime roots, and loopback sink URLs. It clears inherited Cockpit, Agent Mail, Herdr,
  XDG, and Python loader controls before `execve`.
- It binds `127.0.0.1:0`, listens before exec, and rejects reserved production ports.
  The inherited lock FD and listen FD are the only non-stdio descriptors retained across
  exec. The server validates and adopts both; Uvicorn receives the adopted socket through
  `Server.run(sockets=[socket])`.
- Standard output is exactly one starting descriptor with schema version, base URL, PID,
  ready path, and an opaque ready nonce. It contains no runtime paths, command text, or
  environment values.
- `GET /health/ephemeral` is available only in the ephemeral profile. After real lifespan
  startup it returns `ready: true` together with the descriptor's nonce, PID, and port.
- Ephemeral lifespan still prepares the Registry, initializes and closes the five
  foundation stores, and creates then closes the WorkspaceTerminalController before the
  foundation stores close. It does not start external pollers, pending-task recovery, zoom
  release, state-client shutdown, or Herdr/service cleanup.

## Cleanup

The caller owns cleanup. It sends a signal only to the started process group, waits for
reap, and removes only the runtime root it created. The launcher and server do not scan
ports, use systemd, invoke `pkill`, or remove a root while its lock may still be held.

## Test Boundary

The ordinary harness tests run real `Popen`, `execve`, socket handoff, Uvicorn, and
lifespan normal paths. Host/Origin, token, FD-forgery, path/preseed, TOCTOU, and other
adversarial probes are reserved for the sensitive review gate.

The ordinary tests prove the normal profile keeps external modes disabled and exercises
the real lifespan without test-only hooks. Complete zero-activity evidence for external
pollers, recovery, and cleanup belongs to the frozen sensitive S9 gate; ordinary tests do
not add observability backdoors or monkeypatch lifecycle dependencies for that purpose.
