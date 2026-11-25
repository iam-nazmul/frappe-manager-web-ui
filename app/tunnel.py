"""Cloudflare Tunnel: publish sites to the internet without opening ports.

The ``cloudflared`` CLI and the connector both run from the official Docker image. Account files
(cert.pem and <tunnel-id>.json) live in ``config.CLOUDFLARED_DIR`` (shared with a host install);
the UI's own state and the generated connector config live in ``config.TUNNEL_DIR``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, docker, traefik
from .jobs import Job, JobError

STATE_FILE = config.TUNNEL_DIR / "state.json"
CONFIG_FILE = config.TUNNEL_DIR / "config.yml"
# /home/nonroot in the image is not traversable by the host uid we run as, so use a neutral HOME.
HOME = "/fdm"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        st = {}
    return {"tunnel_id": "", "tunnel_name": "", "hostnames": [], **st}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, indent=2))


def cert_path() -> Path:
    return config.CLOUDFLARED_DIR / "cert.pem"


def creds_path(tunnel_id: str) -> Path:
    return config.CLOUDFLARED_DIR / f"{tunnel_id}.json"


def public_hostnames() -> list[str]:
    return load_state()["hostnames"]


def _user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def _cli_args(*args: str, writable: bool = False) -> list[str]:
    # Create it ourselves: a missing bind-mount source would be created by Docker as root.
    config.CLOUDFLARED_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    mount = f"{config.CLOUDFLARED_DIR}:{HOME}/.cloudflared" + ("" if writable else ":ro")
    return [docker.docker_bin(), "run", "--rm", "--user", _user(), "-e", f"HOME={HOME}", "-v", mount,
            config.CLOUDFLARED_IMAGE, "tunnel", *args]


async def cli(*args: str, writable: bool = False, timeout: float = 90) -> docker.Result:
    return await docker.run(_cli_args(*args, writable=writable), timeout=timeout)


def _host_processes() -> list[str]:
    """cloudflared processes running directly on the host (not in a container)."""
    if not Path("/proc").is_dir():
        return _host_processes_pgrep()
    found = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if (proc / "comm").read_text().strip() != "cloudflared":
                continue
            cgroup = (proc / "cgroup").read_text()
            if "docker" in cgroup or "containerd" in cgroup or "libpod" in cgroup:
                continue
            found.append((proc / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip())
        except OSError:
            continue
    return found


def _host_processes_pgrep() -> list[str]:
    # macOS: Docker Desktop runs containers inside a VM, so every match here is a host process.
    try:
        r = subprocess.run(["pgrep", "-fl", "cloudflared"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split(" ", 1)[-1] for line in r.stdout.splitlines() if line.strip()]


async def status() -> dict:
    st = load_state()
    found = await docker.inspect([config.TUNNEL_CONTAINER])
    summ = docker.container_summary(found[0]) if found else None
    return {
        **st,
        "logged_in": cert_path().exists(),
        "cloudflared_dir": str(config.CLOUDFLARED_DIR),
        "has_credentials": bool(st["tunnel_id"]) and creds_path(st["tunnel_id"]).exists(),
        "exists": bool(found),
        "running": bool(summ and summ["running"]),
        "container": summ,
        "image": config.CLOUDFLARED_IMAGE,
        "host_processes": _host_processes(),
    }


async def list_tunnels() -> list[dict]:
    if not cert_path().exists():
        return []
    r = await cli("list", "--output", "json")
    if not r.ok:
        raise RuntimeError(r.text or "cloudflared tunnel list failed")
    out = []
    for t in json.loads(r.out or "[]") or []:
        out.append({"id": t["id"], "name": t["name"], "created": (t.get("created_at") or "")[:19].replace("T", " "),
                    "connections": len(t.get("connections") or []),
                    "has_credentials": creds_path(t["id"]).exists()})
    return out


async def _origin() -> str:
    """Traefik's in-network HTTP address (its container port behind the published HTTP port)."""
    tst = await traefik.status()
    for key, hosts in (tst.get("ports") or {}).items():
        if str(tst.get("http_port")) in hosts:
            return f"http://{config.TRAEFIK_CONTAINER}:{key.split('/')[0]}"
    return f"http://{config.TRAEFIK_CONTAINER}:80"


def render(st: dict, origin: str) -> str:
    lines = [f"tunnel: {st['tunnel_id']}",
             f"credentials-file: {HOME}/.cloudflared/{st['tunnel_id']}.json",
             "no-autoupdate: true",
             "ingress:"]
    for host in st["hostnames"]:
        lines += [f"  - hostname: {host}", f"    service: {origin}"]
    lines.append("  - service: http_status:404")
    return "\n".join(lines) + "\n"


def config_text() -> str:
    try:
        return CONFIG_FILE.read_text()
    except OSError:
        return ""


# --------------------------------------------------------------------------- jobs

async def login(job: Job) -> dict:
    cert = cert_path()
    backup = None
    if cert.exists():
        backup = cert.with_name(f"cert.pem.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        cert.rename(backup)
        await job.log(f"Moved the existing certificate to {backup.name}")
    await job.step("Starting Cloudflare login")
    await job.log("Open the link below, sign in and pick the domain (zone) your sites use. Waiting for you…")
    code = await docker.stream(_cli_args("login", writable=True), job.log)
    if code != 0 or not cert.exists():
        if backup and not cert.exists():
            backup.rename(cert)
            await job.log("Restored the previous certificate")
        raise JobError("Login did not complete")
    await job.ok(f"Logged in - certificate saved to {cert}")
    return {}


async def use_tunnel(job: Job, name: str) -> dict:
    """Use the tunnel called ``name``, creating it if needed, then re-point DNS and restart the connector."""
    if not NAME_RE.match(name):
        raise JobError("Tunnel name: letters, digits, '-', '_' or '.', up to 63 characters")
    if not cert_path().exists():
        raise JobError("Log in to Cloudflare first")
    await job.step("Looking up tunnels")
    existing = next((t for t in await list_tunnels() if t["name"] == name), None)
    if existing:
        tid = existing["id"]
        if not existing["has_credentials"]:
            raise JobError(f"Tunnel '{name}' exists but its credentials file {creds_path(tid)} is missing. "
                           "Copy it there or create a tunnel with a new name.")
        await job.ok(f"Using existing tunnel {name} ({tid})")
    else:
        await job.step(f"Creating tunnel {name}")
        code_lines: list[str] = []

        async def collect(line: str) -> None:
            code_lines.append(line)
            await job.log(line)

        if await docker.stream(_cli_args("create", name, writable=True), collect) != 0:
            raise JobError("cloudflared tunnel create failed")
        m = next((UUID_RE.search(x) for x in reversed(code_lines) if UUID_RE.search(x)), None)
        if not m:
            raise JobError("Could not read the new tunnel id")
        tid = m.group(1)
        await job.ok(f"Created tunnel {name} ({tid})")
    st = load_state()
    changed = st["tunnel_id"] != tid
    st.update(tunnel_id=tid, tunnel_name=name)
    save_state(st)
    if changed:
        for host in st["hostnames"]:
            await _route_dns(job, tid, host)
    if st["hostnames"] or (await docker.inspect([config.TUNNEL_CONTAINER])):
        await apply(job)
    return {}


async def _route_dns(job: Job, tunnel_id: str, hostname: str) -> None:
    await job.step(f"Pointing DNS {hostname} → tunnel")
    r = await cli("route", "dns", "--overwrite-dns", tunnel_id, hostname)
    await job.log(r.text)
    if not r.ok:
        raise JobError(f"Could not create the DNS record for {hostname}. Is its domain in the Cloudflare "
                       "account (zone) you logged in with?")


async def apply(job: Job) -> dict:
    """Write the connector config and (re)create the cloudflared container."""
    st = load_state()
    if not st["tunnel_id"]:
        raise JobError("Choose or create a tunnel first")
    if not creds_path(st["tunnel_id"]).exists():
        raise JobError(f"Tunnel credentials {creds_path(st['tunnel_id'])} not found")
    origin = await _origin()
    CONFIG_FILE.write_text(render(st, origin))
    await job.step(f"Wrote {CONFIG_FILE} ({len(st['hostnames'])} hostname(s) → {origin})")
    await docker.ensure_network(config.TRAEFIK_NETWORK)
    if await docker.inspect([config.TUNNEL_CONTAINER]):
        await docker.docker("rm", "-f", config.TUNNEL_CONTAINER, timeout=60)
    await job.step("Starting the cloudflared connector")
    r = await docker.docker(
        "run", "-d", "--name", config.TUNNEL_CONTAINER, "--restart", "unless-stopped",
        "--network", config.TRAEFIK_NETWORK, "--user", _user(), "-e", f"HOME={HOME}",
        "-v", f"{config.CLOUDFLARED_DIR}:{HOME}/.cloudflared:ro", "-v", f"{config.TUNNEL_DIR}:{HOME}/conf:ro",
        config.CLOUDFLARED_IMAGE, "tunnel", "--config", f"{HOME}/conf/config.yml", "run", timeout=300)
    if not r.ok:
        raise JobError(r.text or "docker run failed")
    for _ in range(30):
        logs = await docker.docker("logs", config.TUNNEL_CONTAINER, timeout=15)
        if "Registered tunnel connection" in logs.text:
            await job.ok("Connector is connected to Cloudflare")
            break
        if not (await docker.inspect([config.TUNNEL_CONTAINER]) or [{}])[0].get("State", {}).get("Running"):
            await job.log(logs.text[-3000:])
            raise JobError("The cloudflared container stopped - see its log above")
        await asyncio.sleep(1)
    else:
        await job.warn("No connection registered after 30s - check the connector logs")
    if _host_processes():
        await job.warn("cloudflared also runs on the host. If it serves the same tunnel with another config, "
                       "requests are split between both - stop it (sudo systemctl disable --now cloudflared).")
    return {}


async def control(action: str) -> docker.Result:
    if action not in ("start", "stop", "restart"):
        raise ValueError("bad action")
    return await docker.docker(action, config.TUNNEL_CONTAINER, timeout=60)


async def publish(job: Job, site: dict) -> dict:
    domain = site["domain"]
    if domain.endswith((".localhost", ".local", ".test")):
        raise JobError(f"{domain} is a local-only name. Publish a site on a real domain in your Cloudflare account.")
    st = load_state()
    if not st["tunnel_id"]:
        raise JobError("Set up the tunnel first (Cloudflare Tunnel page)")
    await _route_dns(job, st["tunnel_id"], domain)
    if domain not in st["hostnames"]:
        st["hostnames"].append(domain)
        save_state(st)
    await apply(job)
    url = f"https://{domain}"
    await _set_host_name(job, site, url)
    await job.log("DNS can take a minute to reach your network; if the browser says the site can't be "
                  "found, wait or try from another network.")
    return {"url": url}


async def unpublish(job: Job, hostname: str, site: dict | None) -> dict:
    st = load_state()
    if hostname not in st["hostnames"]:
        raise JobError(f"{hostname} is not published")
    st["hostnames"].remove(hostname)
    save_state(st)
    await apply(job)
    if site:
        await _set_host_name(job, site, site["url"])
    await job.log(f"The DNS record for {hostname} still points at the tunnel (it now answers 404). "
                  "Delete it in the Cloudflare dashboard if you no longer need it.")
    return {}


async def _set_host_name(job: Job, site: dict, url: str) -> None:
    await job.step(f"Setting the site's host_name to {url}")
    base = ["exec", "-w", config.BENCH_DIR, site["app_container"], "bench", "--site", site["domain"]]
    r = await docker.docker(*base, "set-config", "host_name", url, timeout=120)
    if not r.ok:
        await job.warn(f"Could not set host_name (is the site running?): {r.text}")
        return
    await docker.docker(*base, "clear-cache", timeout=120)
