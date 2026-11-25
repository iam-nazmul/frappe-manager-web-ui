"""Cloudflare Tunnel: publish sites to the internet without opening ports.

The ``cloudflared`` CLI and the connector both run from the official Docker image. Account files
(cert.pem and <tunnel-id>.json) live in ``config.CLOUDFLARED_DIR`` on this machine (shared with a host
install), so login, tunnel creation and DNS changes always run on the local Docker. Each server has its
own tunnel state and config in ``config.TUNNEL_DIR``; a remote server's connector gets only its config and
that tunnel's credentials file, uploaded to ``<remote_root>/tunnel``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config, docker, servers, traefik
from .jobs import Job, JobError

# /home/nonroot in the image is not traversable by the host uid we run as, so use a neutral HOME.
HOME = "/fdm"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


def state_file() -> Path:
    return config.TUNNEL_DIR / "state.json"


def config_file() -> Path:
    return config.TUNNEL_DIR / "config.yml"


def load_state() -> dict:
    try:
        st = json.loads(state_file().read_text())
    except (OSError, ValueError):
        st = {}
    return {"tunnel_id": "", "tunnel_name": "", "hostnames": [], **st}


def save_state(st: dict) -> None:
    state_file().write_text(json.dumps(st, indent=2))


def cert_path() -> Path:
    return config.CLOUDFLARED_DIR / "cert.pem"


def zones_dir() -> Path:
    """One login certificate per Cloudflare zone, named <zone>.pem (cert.pem is only the latest login)."""
    return config.CLOUDFLARED_DIR / "zones"


def _cert_token(path: Path) -> dict:
    m = re.search(r"-----BEGIN ARGO TUNNEL TOKEN-----\s*(.+?)\s*-----END ARGO TUNNEL TOKEN-----",
                  path.read_text(), re.S)
    return json.loads(base64.b64decode("".join(m.group(1).split()))) if m else {}


def _zone_name(token: dict) -> str:
    req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/zones/{token['zoneID']}",
                                 headers={"Authorization": f"Bearer {token['apiToken']}", "User-Agent": "fdm"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["result"]["name"]


async def index_certs() -> list[str]:
    """File every login certificate (cert.pem and its backups) under zones/<zone>.pem; return the zones."""
    zd = zones_dir()
    zd.mkdir(mode=0o700, exist_ok=True)
    index_file = zd / "index.json"
    try:
        index = json.loads(index_file.read_text())
    except (OSError, ValueError):
        index = {}
    for cert in sorted(config.CLOUDFLARED_DIR.glob("cert.pem*"), key=lambda p: p.stat().st_mtime):
        try:
            token = _cert_token(cert)
        except (OSError, ValueError):
            continue
        zid = token.get("zoneID")
        if not zid:
            continue
        if zid not in index:
            try:
                index[zid] = await asyncio.to_thread(_zone_name, token)
            except Exception:  # noqa: BLE001 - an expired or offline login is just skipped
                continue
        name = index[zid]
        if re.fullmatch(r"[a-z0-9.-]+", name):
            target = zd / f"{name}.pem"
            if not target.exists() or target.stat().st_mtime < cert.stat().st_mtime:
                shutil.copyfile(cert, target)
                target.chmod(0o600)
    index_file.write_text(json.dumps(index, indent=2))
    return sorted(p.stem for p in zd.glob("*.pem"))


def zone_for(hostname: str) -> str:
    """The logged-in zone that ``hostname`` belongs to (longest match), or ''."""
    matches = [p.stem for p in zones_dir().glob("*.pem") if hostname == p.stem or hostname.endswith("." + p.stem)]
    return max(matches, key=len, default="")


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
    with servers.on_local():
        return await docker.run(_cli_args(*args, writable=writable), timeout=timeout)


async def _cli_stream(args: list[str], on_line) -> int:
    with servers.on_local():
        return await docker.stream(_cli_args(*args, writable=True), on_line)


def _used_by_other_server(tunnel_id: str) -> str:
    """Name of another server whose connector already runs this tunnel, if any."""
    me = servers.current().name
    for srv in servers.load_all():
        if srv.name == me:
            continue
        try:
            if json.loads((srv.data_dir / "tunnel" / "state.json").read_text()).get("tunnel_id") == tunnel_id:
                return srv.name
        except (OSError, ValueError):
            continue
    return ""


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
        "zones": sorted(p.stem for p in zones_dir().glob("*.pem")),
        "cloudflared_dir": str(config.CLOUDFLARED_DIR),
        "has_credentials": bool(st["tunnel_id"]) and creds_path(st["tunnel_id"]).exists(),
        "exists": bool(found),
        "running": bool(summ and summ["running"]),
        "container": summ,
        "image": config.CLOUDFLARED_IMAGE,
        "host_processes": _host_processes() if servers.current().local else [],
        "remote": not servers.current().local,
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


def render(st: dict, origin: str, creds_dir: str = f"{HOME}/.cloudflared") -> str:
    lines = [f"tunnel: {st['tunnel_id']}",
             f"credentials-file: {creds_dir}/{st['tunnel_id']}.json",
             "no-autoupdate: true",
             "ingress:"]
    for host in st["hostnames"]:
        lines += [f"  - hostname: {host}", f"    service: {origin}"]
    lines.append("  - service: http_status:404")
    return "\n".join(lines) + "\n"


def config_text() -> str:
    try:
        return config_file().read_text()
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
    code = await _cli_stream(["login"], job.log)
    if code != 0 or not cert.exists():
        if backup and not cert.exists():
            backup.rename(cert)
            await job.log("Restored the previous certificate")
        raise JobError("Login did not complete")
    await job.ok(f"Logged in - certificate saved to {cert}")
    zones = await index_certs()
    await job.ok(f"Domains you can publish on: {', '.join(zones) or '(none found)'}. Log in again to add another "
                 "domain; each site uses the login for its own domain.")
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
        other = _used_by_other_server(tid)
        if other:
            raise JobError(f"Tunnel '{name}' already runs on server '{other}'. Cloudflare would split requests "
                           "between both servers - create a separate tunnel for this server.")
        await job.ok(f"Using existing tunnel {name} ({tid})")
    else:
        await job.step(f"Creating tunnel {name}")
        code_lines: list[str] = []

        async def collect(line: str) -> None:
            code_lines.append(line)
            await job.log(line)

        if await _cli_stream(["create", name], collect) != 0:
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
    zones = await index_certs()
    zone = zone_for(hostname)
    if not zone:
        raise JobError(f"No Cloudflare login for the domain of {hostname} (logged in for: "
                       f"{', '.join(zones) or 'none'}). On the Cloudflare Tunnel page press Log in and pick "
                       f"{'.'.join(hostname.split('.')[-2:])}.")
    await job.step(f"Pointing DNS {hostname} → tunnel (Cloudflare zone {zone})")
    r = await cli("--origincert", f"{HOME}/.cloudflared/zones/{zone}.pem",
                  "route", "dns", "--overwrite-dns", tunnel_id, hostname)
    await job.log(r.text)
    if not r.ok:
        raise JobError(f"Could not create the DNS record for {hostname} in zone {zone}")
    m = re.search(r"Added CNAME (\S+)|(\S+) is already configured|(\S+) updated to route", r.text)
    routed = next((g for g in (m.groups() if m else ()) if g), "")
    if routed and routed.rstrip(".") != hostname:
        raise JobError(f"Cloudflare created {routed} instead of {hostname}. Delete that record in the "
                       "Cloudflare dashboard and check which domain you logged in with.")


async def apply(job: Job) -> dict:
    """Write the connector config and (re)create the cloudflared container."""
    srv = servers.current()
    st = load_state()
    if not st["tunnel_id"]:
        raise JobError("Choose or create a tunnel first")
    creds = creds_path(st["tunnel_id"])
    if not creds.exists():
        raise JobError(f"Tunnel credentials {creds} not found")
    origin = await _origin()
    config.TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    if srv.local:
        text = render(st, origin)
        user = _user()
        mounts = ["-v", f"{config.CLOUDFLARED_DIR}:{HOME}/.cloudflared:ro", "-v", f"{config.TUNNEL_DIR}:{HOME}/conf:ro"]
    else:
        text = render(st, origin, creds_dir=f"{HOME}/conf")
        remote_dir = f"{srv.remote_root}/tunnel"
        await servers.upload_files(srv, {"config.yml": text.encode(), creds.name: creds.read_bytes()}, remote_dir)
        await job.log(f"Uploaded the connector config and tunnel credentials to {srv.name}:{remote_dir}")
        user = await servers.remote_ids(srv)
        mounts = ["-v", f"{remote_dir}:{HOME}/conf:ro"]
    config_file().write_text(text)
    await job.step(f"Wrote {config_file()} ({len(st['hostnames'])} hostname(s) → {origin})")
    await docker.ensure_network(config.TRAEFIK_NETWORK)
    if await docker.inspect([config.TUNNEL_CONTAINER]):
        await docker.docker("rm", "-f", config.TUNNEL_CONTAINER, timeout=60)
    await job.step("Starting the cloudflared connector")
    r = await docker.docker(
        "run", "-d", "--name", config.TUNNEL_CONTAINER, "--restart", "unless-stopped",
        "--network", config.TRAEFIK_NETWORK, "--user", user, "-e", f"HOME={HOME}", *mounts,
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
    if srv.local and _host_processes():
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
    if not cert_path().exists():
        raise JobError("Log in to Cloudflare first (Cloudflare Tunnel page)")
    if not load_state()["tunnel_id"]:
        await use_tunnel(job, f"fdm-{servers.current().name}")
    st = load_state()
    other =_used_by_other_server(st["tunnel_id"])
    if other:
        raise JobError(f"This server's tunnel also runs on '{other}' - choose a separate tunnel for this server")
    if site.get("web_server") == "development":
        await job.warn("This site uses the development web server (bench serve), which shows debugger pages "
                       "on errors. For a public site, recreate it with the Production web server.")
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
