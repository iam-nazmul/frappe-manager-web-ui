"""Shared Traefik reverse proxy: configuration, setup and repair."""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import config, docker
from .jobs import Job, JobError

CONFIG_FILE = config.TRAEFIK_DIR / "config.json"
COMPOSE_FILE = config.TRAEFIK_DIR / "docker-compose.yml"
ENV_FILE = config.TRAEFIK_DIR / ".env"
EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[^@\s'\"]+$")

yaml_env = Environment(
    loader=FileSystemLoader(config.TEMPLATES_DIR / "compose"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,  # noqa: S701 - renders YAML, not HTML; inputs are validated
)

DEFAULTS = {
    "mode": "local",  # local | server
    "http_port": 8081,
    "dashboard_port": 8080,
    "ssl": False,
    "email": "",
    "challenge": "http",  # http | cloudflare
    "staging": False,
}


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def suggest_port(start: int = 8081, end: int = 8099, skip: set[int] | None = None) -> int | None:
    for p in range(start, end + 1):
        if p not in (skip or set()) and port_free(p):
            return p
    return None


async def status() -> dict:
    cfg = load_config()
    found = await docker.inspect([config.TRAEFIK_CONTAINER])
    net = await docker.network_exists(config.TRAEFIK_NETWORK)
    st: dict = {"config": cfg, "network": net, "exists": bool(found), "running": False,
                "managed": COMPOSE_FILE.exists(), "compose_file": str(COMPOSE_FILE)}
    if found:
        c = found[0]
        summ = docker.container_summary(c)
        ports = c.get("NetworkSettings", {}).get("Ports") or {}
        published = {k: [b.get("HostPort") for b in (v or [])] for k, v in ports.items()}
        st.update(running=summ["running"], container=summ, ports=published,
                  image=summ["image"], foreign=not st["managed"] or
                  (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") != "traefik")
        # Detect the ports actually in use (legacy script setups use different ports)
        for key, hosts in published.items():
            if key.startswith("80/") and hosts:
                st["live_http_port"] = int(hosts[0])
            if key.startswith("8080/") and hosts:
                st["live_dashboard_port"] = int(hosts[0])
            if key.startswith("443/") and hosts:
                st["live_https"] = True
            # Legacy local scripts publish 8081:8081 with entrypoint :8081
            if re.match(r"^80[89]\d/tcp$", key) and key != "8080/tcp" and hosts:
                st.setdefault("live_http_port", int(hosts[0]))
    http_port = st.get("live_http_port", cfg["http_port"])
    st["http_port"] = http_port
    st["dashboard_url"] = f"http://localhost:{st.get('live_dashboard_port', cfg['dashboard_port'])}/dashboard/"
    return st


def site_url(domain: str, ssl: bool, http_port: int) -> str:
    if ssl:
        return f"https://{domain}"
    return f"http://{domain}" + ("" if http_port == 80 else f":{http_port}")


def render(cfg: dict) -> str:
    return yaml_env.get_template("traefik.yml.j2").render(
        image=config.TRAEFIK_IMAGE,
        ssl=cfg["ssl"],
        email=cfg["email"],
        challenge=cfg["challenge"],
        staging=cfg["staging"],
        http_port=cfg["http_port"],
        dashboard_port=cfg["dashboard_port"],
        dashboard_bind="127.0.0.1",
    )


def validate(form: dict) -> dict:
    cfg = load_config()
    mode = form.get("mode", cfg["mode"])
    if mode not in ("local", "server"):
        raise ValueError("mode must be 'local' or 'server'")
    ssl = bool(form.get("ssl")) and mode == "server"
    try:
        http_port = int(form.get("http_port") or (80 if mode == "server" else cfg["http_port"]))
        dash = int(form.get("dashboard_port") or cfg["dashboard_port"])
    except ValueError as exc:
        raise ValueError("Ports must be numbers") from exc
    for p in (http_port, dash):
        if not 1 <= p <= 65535:
            raise ValueError(f"Invalid port {p}")
    if http_port == dash:
        raise ValueError("HTTP port and dashboard port must differ")
    challenge = form.get("challenge", "http")
    if challenge not in ("http", "cloudflare"):
        raise ValueError("Unknown ACME challenge")
    email = (form.get("email") or "").strip()
    if ssl:
        if not EMAIL_RE.match(email):
            raise ValueError("A valid email is required for Let's Encrypt")
        if challenge == "http" and http_port != 80:
            raise ValueError("The Let's Encrypt HTTP challenge requires HTTP port 80")
    token = (form.get("cf_token") or "").strip()
    if token and not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", token):
        raise ValueError("Cloudflare API token looks invalid")
    if ssl and challenge == "cloudflare" and not token and "CF_DNS_API_TOKEN=" not in _read_env():
        raise ValueError("A Cloudflare API token (Zone:DNS:Edit) is required for the DNS challenge")
    return {"mode": mode, "http_port": http_port, "dashboard_port": dash, "ssl": ssl, "email": email,
            "challenge": challenge, "staging": bool(form.get("staging")), "_cf_token": token}


def _read_env() -> str:
    try:
        return ENV_FILE.read_text()
    except OSError:
        return ""


async def setup(job: Job, cfg: dict) -> dict:
    token = cfg.pop("_cf_token", "")
    await job.step("Checking Docker")
    info = await docker.info()
    if not info["ok"]:
        raise JobError(f"Docker is not available: {info['error']}")
    await job.ok(f"Docker {info['server_version']} with compose {info['compose_version']}")

    await job.step(f"Ensuring the '{config.TRAEFIK_NETWORK}' network")
    created = await docker.ensure_network(config.TRAEFIK_NETWORK)
    await job.ok("Created network" if created else "Network already exists")

    existing = await docker.inspect([config.TRAEFIK_CONTAINER])
    if existing:
        await job.step("Removing the existing Traefik container (sites keep running; routing pauses briefly)")
        r = await docker.docker("rm", "-f", config.TRAEFIK_CONTAINER, timeout=60)
        await job.log(r.text)

    await job.step("Checking ports")
    ports = [cfg["http_port"], cfg["dashboard_port"]] + ([443] if cfg["ssl"] else [])
    busy = [p for p in ports if not port_free(p)]
    if busy:
        alt = suggest_port(skip={cfg["dashboard_port"]}) if cfg["mode"] == "local" else None
        hint = f" Try HTTP port {alt}." if alt and cfg["http_port"] in busy else ""
        raise JobError(f"Port(s) {', '.join(map(str, busy))} already in use on this machine.{hint}")
    await job.ok(f"Ports free: {', '.join(map(str, ports))}")

    await job.step("Writing Traefik configuration")
    (config.TRAEFIK_DIR / "letsencrypt").mkdir(exist_ok=True)
    COMPOSE_FILE.write_text(render(cfg))
    if token:
        ENV_FILE.write_text(f"CF_DNS_API_TOKEN={token}\n")
        ENV_FILE.chmod(0o600)
    elif not ENV_FILE.exists():
        ENV_FILE.write_text("")
    save_config(cfg)
    await job.log(f"Wrote {COMPOSE_FILE}")

    await job.step("Starting Traefik")
    comp = await docker.compose_cmd()
    code = await docker.stream([*comp, "-f", str(COMPOSE_FILE), "--project-directory",
                                str(config.TRAEFIK_DIR), "up", "-d", "--pull", "missing"], job.log)
    if code != 0:
        raise JobError("Traefik failed to start - see the log above")
    st = await status()
    if not st["running"]:
        logs = await docker.docker("logs", "--tail", "40", config.TRAEFIK_CONTAINER)
        await job.log(logs.text)
        raise JobError("Traefik container is not running")
    await job.ok(f"Traefik is running - HTTP on port {cfg['http_port']}"
                 + (", HTTPS on 443" if cfg["ssl"] else ""))
    await job.log(f"Dashboard: {st['dashboard_url']} (bound to 127.0.0.1 only)")
    return {"dashboard_url": st["dashboard_url"]}


async def control(action: str) -> docker.Result:
    if action not in ("start", "stop", "restart"):
        raise ValueError("bad action")
    return await docker.docker(action, config.TRAEFIK_CONTAINER, timeout=60)


async def fix_network(job: Job) -> dict:
    """Port of fix-traefik-network.sh: recreate traefik_proxy and reconnect every container."""
    net = config.TRAEFIK_NETWORK
    await job.step("Collecting containers attached to the proxy network")
    connected: list[str] = []
    if await docker.network_exists(net):
        r = await docker.docker("network", "inspect", net, "-f", "{{range .Containers}}{{.Name}} {{end}}")
        connected = r.out.split()
    # Also reconnect every Frappe app container that should be on it
    for c in await docker.all_containers():
        nets = (c.get("NetworkSettings", {}).get("Networks") or {})
        labels = c.get("Config", {}).get("Labels") or {}
        name = c.get("Name", "").lstrip("/")
        if (labels.get("com.docker.compose.service") == "app" or name == config.TRAEFIK_CONTAINER
                or net in nets) and name not in connected:
            connected.append(name)
    await job.log(", ".join(connected) or "(none)")

    for name in connected:
        await docker.docker("network", "disconnect", "-f", net, name)
    await job.step("Re-creating the network")
    await docker.docker("network", "rm", net)
    await docker.ensure_network(net)
    await job.ok(f"Network {net} re-created")

    await job.step("Reconnecting containers")
    for name in connected:
        r = await docker.docker("network", "connect", net, name)
        if r.ok:
            await job.ok(f"  {name}")
        else:
            await job.warn(f"  {name}: {r.text}")
    if await docker.inspect([config.TRAEFIK_CONTAINER]):
        await job.step("Restarting Traefik")
        await docker.docker("restart", config.TRAEFIK_CONTAINER, timeout=60)
    await job.log("If the browser shows ERR_NETWORK_CHANGED, clear its cache or restart it.")
    return {}


def compose_text() -> str:
    try:
        return Path(COMPOSE_FILE).read_text()
    except OSError:
        return ""
