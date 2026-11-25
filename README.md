# Frappe/ERPNext Docker Manager — Web UI

A FastAPI + Jinja2 web interface that does everything the shell scripts in this repository do
(`generate_frappe_docker*.sh`, `setup-traefik-local*.sh`, `docker-manager.sh`, `fix-*.sh`), from the browser.

## Quick start

```bash
cd web-ui
./run.sh                 # creates .venv, installs deps, serves http://127.0.0.1:8765
```

Requirements: Docker Engine with the compose plugin (Docker Desktop on macOS, or Docker CE on Linux), and Python 3.10+.
The user running the UI must be able to talk to Docker (be in the `docker` group on Linux). No `sudo` is needed.

### Remote / VPS access

The UI controls Docker, so it is root-equivalent. By default it listens on 127.0.0.1 only. To reach it on a server, prefer an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 you@server      # then open http://127.0.0.1:8765 locally
```

If you must expose it, `run.sh` will not start without a password:

```bash
FDM_HOST=0.0.0.0 FDM_USERNAME=admin FDM_PASSWORD='long-random-secret' ./run.sh
```

Put it behind HTTPS if it is reachable from the internet.

## What you can do from the UI

| Area | Features |
|------|----------|
| **Dashboard** | Docker engine status, Traefik status, all sites (including ones created by the CLI scripts) with live state, quick start/stop |
| **New site wizard** | Local or VPS mode, ERPNext version picker (live from Docker Hub), admin password, developer mode, VS Code bind-mount of `apps/`, HTTPS, resource limits, plus a live log of every step |
| **Traefik** | Install or reconfigure (local custom port with free-port finder, or VPS 80/443), Let's Encrypt through the HTTP-01 or Cloudflare DNS-01 challenge, staging CA, start/stop/restart, logs, network repair |
| **Cloudflare Tunnel** | Publish sites on your Cloudflare domains over HTTPS from any machine (home PC behind a router included): log in, pick or create a tunnel, publish/unpublish sites, connector start/stop/restart and logs |
| **Site → Overview** | Containers with health, per-container start/stop/restart, re-create app, down (keep data), credentials (revealed on request), compose file viewer, `/etc/hosts` helper, publish/unpublish through Cloudflare Tunnel |
| **Site → Processes** | Supervisor status for web, scheduler, workers and websocket; start/stop/restart each or all |
| **Site → Logs** | Every supervisor log, `frappe.log`/`web.error.log`, and app/create-site/db/redis container logs; load or follow live |
| **Site → Apps & bench** | `get-app` (name or git URL plus branch) and install, install/uninstall app, migrate, build, clear cache, scheduler on/off, developer mode, doctor, set admin password, backups (create and download) |
| **Site → Terminal** | Interactive in-browser shell (xterm.js) as `frappe` or `root`, `bench console`, `bench mariadb` |
| **Site → Files & packages** | Upload files into a container, download any file or folder (`.tar`), install apt/apk packages |
| **Site → Maintenance** | Fix DB permissions, fix restart policies, repair proxy network, delete the site (containers, volumes, folder, and optionally the VS Code folder) |
| **Tasks** | Every long operation runs as a background task with a replayable live log; logs are saved to `$FDM_DATA_DIR/jobs/` |

## Publishing a site with Cloudflare Tunnel

Use this to make a site public without opening ports, e.g. from a PC behind a home router. The site's domain must be on Cloudflare.

1. Create the site on its real domain (VPS / server mode), e.g. `erp.example.com`.
2. Open **Cloudflare Tunnel** → **Log in**. Open the link shown in the task log and authorise the domain.
3. Choose an existing tunnel or type a new name → **Use tunnel**.
4. Publish the site there or on its Overview tab. The UI creates the DNS record, starts a `cloudflared` container (restarts with Docker), and sets the site's `host_name` to `https://<domain>`.

The site is online only while this machine is running. You don't need to install `cloudflared` on the host; if one is running there, the Tunnel page warns you.

On a Mac, the same steps work with Docker Desktop. Turn on "Start Docker Desktop when you sign in", and remember that the site goes offline while the Mac sleeps. A hostname can be published from only one machine at a time; publishing it elsewhere moves its DNS record there.

## Generated stack (per site)

Same 4-container layout as the scripts, written to `~/frappe-docker-manager/sites/<site_name>/`:

```
docker-compose.yml     app · create-site · db (MariaDB 11.8) · redis (6.2)
.env                   ERPNext version, random DB/Redis passwords, admin password (chmod 600)
supervisor/            supervisord.conf + frappe.conf (mounted read-only into the app container)
site.json              metadata used by the UI
```

- Container names (`<site>-app`, `-db`, `-redis`, `-create-site`) and the Supervisor paths are the same as the scripts', so
  `docker-manager.sh` and the commands in the main README still work on UI-created sites.
- `bench new-site --mariadb-user-host-login-scope=%` replaces the scripts' manual grant workaround. "Fix DB permissions" is still available.
- MariaDB is **11.8** (as in frappe_docker), not 10.6 as in the scripts. The MariaDB client in ERPNext v16 images requires TLS, and only 11.x servers provide it by default.
- Traefik runs **v3.6**. Docker Engine 29+ rejects the API version that the scripts' Traefik v2.10 uses.
- No hard-coded subnets: Docker allocates them, so sites can't collide.

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FDM_HOST` / `FDM_PORT` | `127.0.0.1` / `8765` | Listen address |
| `FDM_USERNAME` / `FDM_PASSWORD` | — / — | HTTP Basic auth (required when not on localhost) |
| `FDM_DATA_DIR` | `~/frappe-docker-manager` | Compose files, Traefik config, task logs |
| `FDM_DEV_DIR` | `~/frappe-docker` | Where VS Code `apps/` folders are created (same as the scripts) |
| `FDM_TRAEFIK_IMAGE` | `traefik:v3.6` | Traefik image |
| `FDM_MARIADB_IMAGE` / `FDM_REDIS_IMAGE` | `mariadb:11.8` / `redis:6.2-alpine` | Backing service images |
| `FDM_CLOUDFLARED_DIR` | `~/.cloudflared` | Cloudflare login certificate and tunnel credentials (shared with a host `cloudflared`) |
| `FDM_CLOUDFLARED_IMAGE` | `cloudflare/cloudflared:2026.11.2` | Tunnel connector image |
| `FDM_RELOAD` | — | Set to `1` for auto-reload while developing the UI |

## Project layout

```
web-ui/
├── run.sh                    launcher (venv + uvicorn)
├── requirements.txt
└── app/
    ├── main.py               FastAPI routes (pages, JSON APIs, SSE, WebSocket)
    ├── sites.py              site discovery, creation job, operations
    ├── traefik.py            Traefik config/setup/network repair
    ├── tunnel.py             Cloudflare Tunnel login, tunnel choice, connector, publish/unpublish
    ├── docker.py             async docker CLI wrappers
    ├── jobs.py               background tasks with live logs
    ├── terminal.py           PTY ↔ WebSocket bridge for docker exec
    ├── security.py           Basic auth + same-origin guard
    ├── versions.py           ERPNext tags from Docker Hub
    ├── templates/            Jinja2 pages + compose/supervisor templates
    └── static/               app.css, app.js
```

## Security notes

- Every non-GET request and every WebSocket must come from the UI's own origin. This blocks cross-site requests from other tabs.
- All docker calls use argument lists (no shell). Site names, app names, git URLs, package names and process names are validated.
- Passwords never appear on the dashboard's HTML; credentials are revealed only on request on the site page.
- The API docs are available at `/api/docs`.
# frappe-manager-web-ui


01919-944064