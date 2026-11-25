"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# Where the UI keeps generated compose files, .env files, Traefik config and job logs.
DATA_DIR = Path(os.environ.get("FDM_DATA_DIR", Path.home() / "frappe-docker-manager")).expanduser()
SITES_DIR = DATA_DIR / "sites"
TRAEFIK_DIR = DATA_DIR / "traefik"
JOBS_DIR = DATA_DIR / "jobs"
TUNNEL_DIR = DATA_DIR / "tunnel"

# Host folder that receives the bench "apps" directory for local development (VS Code editing).
DEV_APPS_ROOT = Path(os.environ.get("FDM_DEV_DIR", Path.home() / "frappe-docker")).expanduser()

# Optional HTTP Basic auth. Strongly recommended whenever the UI listens on anything but 127.0.0.1.
AUTH_USER = os.environ.get("FDM_USERNAME", "")
AUTH_PASSWORD = os.environ.get("FDM_PASSWORD", "")

TRAEFIK_NETWORK = "traefik_proxy"
TRAEFIK_CONTAINER = "traefik"
TRAEFIK_IMAGE = os.environ.get("FDM_TRAEFIK_IMAGE", "traefik:v3.6")
MARIADB_IMAGE = os.environ.get("FDM_MARIADB_IMAGE", "mariadb:11.8")
REDIS_IMAGE = os.environ.get("FDM_REDIS_IMAGE", "redis:6.2-alpine")
ERPNEXT_IMAGE = "frappe/erpnext"

SUPERVISOR_CONF = "/home/frappe/supervisor/supervisord.conf"
SUPERVISORCTL = "/home/frappe/.local/bin/supervisorctl"
BENCH_DIR = "/home/frappe/frappe-bench"

# Cloudflare Tunnel. Account files (cert.pem, <tunnel-id>.json) are shared with a host cloudflared install.
CLOUDFLARED_DIR = Path(os.environ.get("FDM_CLOUDFLARED_DIR", Path.home() / ".cloudflared")).expanduser()
CLOUDFLARED_IMAGE = os.environ.get("FDM_CLOUDFLARED_IMAGE", "cloudflare/cloudflared:2026.11.2")
TUNNEL_CONTAINER = "cloudflared"

for d in (SITES_DIR, TRAEFIK_DIR, JOBS_DIR, TUNNEL_DIR):
    d.mkdir(parents=True, exist_ok=True)
