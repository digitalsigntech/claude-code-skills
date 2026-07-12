# Boot / Autostart Pattern

A small, dependency-free set of shell templates for reliably starting
long-running services on boot and keeping them up. It covers three common
service shapes:

- **Health-checked servers** — a model server, an API worker: launched
  idempotently and waited on until each `/health` endpoint answers.
- **A remote-connected gateway** — a bot or chat bridge: single-instance via
  `flock`, waits for DNS before launching.
- **A persistent-connection worker** — an IMAP IDLE watcher, a websocket
  listener, a queue consumer: single-instance, plus a *socket-aware* watchdog
  that detects a wedged-but-alive connection, not just a dead process.

The whole thing is plain `bash` + `cron` (`@reboot` + `*/N` watchdogs). No
systemd, no supervisor daemon, no external packages.

## How it works

The pattern rests on three ideas:

| Idea | What it buys you | Where |
|------|------------------|-------|
| `flock -n` single-instance lock | The *same* launcher script can be the `@reboot` starter, the watchdog, and a manual start — it's an instant no-op if the service already runs. | `start_gateway.sh`, `start_watcher.sh` |
| DNS/network wait before launch | A fast boot often fires `@reboot` before DNS is up; the script waits (up to 5 min) instead of crash-looping. | `start_gateway.sh`, `start_watcher.sh` |
| `@reboot` + `*/N` watchdog cron | Boot starts it; the watchdog re-starts it within N minutes if it dies. For socket clients, the watchdog also checks the TCP connection is live. | `crontab.example`, `watchdog.sh` |

### Files

| File | Role |
|------|------|
| `src/start_servers.sh` | Launch one or more health-checked servers (idempotent; PID files; bounded health-wait loop). |
| `src/stop_servers.sh` | Stop them via PID files, with a name-based `pkill` fallback. |
| `src/start_gateway.sh` | Single-instance (`flock`) launcher for a foreground, remote-connected service; DNS-wait; `exec`s the real process so it inherits the shell PID. |
| `src/start_watcher.sh` | Single-instance launcher for a persistent-connection worker. Doubles as its own watchdog target. |
| `src/watchdog.sh` | Socket-aware watchdog: restarts the worker if the process is gone **or** if its ESTABLISHED socket to the remote port has been absent past a grace window. |
| `crontab.example` | Wires `@reboot` launchers + `*/5` and `*/1` watchdogs together. |

### Why a socket-aware watchdog?

A long-lived socket client (IMAP IDLE, a websocket) can keep its process alive
while the underlying TCP connection dies silently — no new events ever arrive
and nothing crashes. `watchdog.sh` checks for an ESTABLISHED socket on the
remote port owned by the worker's PID (`ss -tnp state established`), and only
restarts after the socket has been missing for `STALE_SECS` (default 90s) so a
brief reconnect isn't punished.

## Prerequisites

- `bash`, `cron`, and standard coreutils.
- `flock` (from `util-linux`) — present on essentially all Linux.
- `curl` — for the server health checks in `start_servers.sh`.
- `getent` — for the DNS wait (glibc; standard on Linux).
- `ss` (from `iproute2`) — for the socket-aware watchdog.
- Your actual service binaries / Python scripts and, if used, a project
  virtualenv at `~/myproject/venv`.

## Install / setup

1. Copy the scripts you need into your project directory (referred to here as
   `~/myproject`) and make them executable:
   ```bash
   cp src/*.sh ~/myproject/
   chmod +x ~/myproject/*.sh
   ```
2. Edit each script for your service:
   - `start_servers.sh`: set `SERVER_BIN`, and replace the two example
     `start NAME PORT ...` lines and the health-wait list with your services.
   - `start_gateway.sh`: set `REMOTE_HOST`, and change the final `exec` line to
     launch your gateway.
   - `start_watcher.sh`: set `REMOTE_HOST` and the final `exec` line to your
     worker script.
   - `watchdog.sh`: set `REMOTE_PORT` (the port your worker keeps open) and
     `PROC_MATCH` (a `pgrep` pattern that uniquely identifies it).
3. Install the cron entries:
   ```bash
   crontab crontab.example   # after editing the paths inside it
   ```
4. Reboot to test the `@reboot` path, or run each launcher by hand first.

## Config

Most knobs are environment variables with sensible defaults, so you can
override without editing the scripts:

| Variable | Used by | Default | Meaning |
|----------|---------|---------|---------|
| `SERVER_BIN` | `start_servers.sh` | `$BASE/bin/my-server` | Path to the server executable. |
| `REMOTE_HOST` | `start_gateway.sh`, `start_watcher.sh` | `api.example-service.com` / `imap.example.com` | Host to wait for DNS on before launch. |
| `PY` | `start_watcher.sh` | `$DIR/venv/bin/python` | Python interpreter for the worker. |
| `REMOTE_PORT` | `watchdog.sh` | `993` | Remote port the worker keeps an ESTABLISHED socket on. |
| `PROC_MATCH` | `watchdog.sh` | `watcher.py` | `pgrep -f` pattern identifying the worker process. |
| `STALE_SECS` | `watchdog.sh` | `90` | Grace period a socket may be missing before a restart. |

Timing constants (health-wait `60 x 2s`, DNS-wait `60 x 5s`) are inline near the
top of each loop; adjust to taste.

## Caveats

- **Templates, not turnkey.** The service commands are placeholders
  (`my-server`, `gateway.py`, `watcher.py`). Point them at your real binaries.
- **Bind address.** `start_servers.sh` binds `127.0.0.1` on purpose. Change the
  `--host` only if you deliberately want the service reachable off-box.
- **`@reboot` needs a running cron daemon.** On minimal images ensure `cron`/
  `crond` is enabled. The DNS-wait covers slow networking, not a disabled cron.
- **Watchdog assumes one match.** `watchdog.sh` takes the first PID from
  `pgrep -f "$PROC_MATCH"`; make the pattern specific enough to match only your
  worker.
- **This is a generic export.** All project-, host-, and product-specific names,
  paths, ports, and credentials from the original have been replaced with
  neutral placeholders. Nothing sensitive is shipped — there are no tokens,
  databases, or config secrets here (this pattern needs none).
