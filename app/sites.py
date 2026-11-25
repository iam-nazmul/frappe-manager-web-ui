"""Frappe/ERPNext sites: discovery, creation and day-to-day operations."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import string
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, docker, traefik, tunnel, versions
from .jobs import Job, JobError

DOMAIN_RE = re.compile(r"^(?=.{4,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
APP_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
GIT_URL_RE = re.compile(r"^(https://[\w.\-]+/[\w.\-/~]+?(\.git)?|git@[\w.\-]+:[\w.\-/~]+?(\.git)?)$")
BRANCH_RE = re.compile(r"^[\w.\-/]{1,100}$")
PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,80}$")
PROGRESS_RE = re.compile(r"^(.*?):?\s*\[[=\s]*\]\s*(\d+)%\s*$")
PROCESS_RE = re.compile(r"^(all|frappe:[a-z0-9_-]+|[a-z0-9_-]+)$")
SERVICES = ("app", "create-site", "db", "redis")
PROGRAMS = ["frappe-web", "frappe-schedule", "frappe-worker-short", "frappe-worker-long",
            "frappe-worker-default", "frappe-websocket"]


def safe_name(domain: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", domain.lower())


def gen_password(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def site_dir(project: str) -> Path:
    return config.SITES_DIR / project


def read_env_file(path: Path) -> dict:
    env: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _container_env(c: dict) -> dict:
    out = {}
    for item in c.get("Config", {}).get("Env") or []:
        if "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------------- discovery

async def discover() -> list[dict]:
    """Every Frappe site: compose projects with a frappe/* app container, plus UI-managed folders."""
    containers = await docker.all_containers()
    tstatus = await traefik.status()
    groups: dict[str, list[dict]] = {}
    for c in containers:
        labels = c.get("Config", {}).get("Labels") or {}
        name = c.get("Name", "").lstrip("/")
        project = labels.get("com.docker.compose.project")
        if not project:
            # unmanaged container named like "<project>-app" from older scripts
            m = re.match(r"^(.+)-(app|db|redis|create-site)$", name)
            project = m.group(1) if m else None
        if project:
            groups.setdefault(project, []).append(c)

    sites: dict[str, dict] = {}
    for project, items in groups.items():
        app = next((c for c in items if c.get("Name", "").lstrip("/") == f"{project}-app"
                    or (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service") == "app"), None)
        if not app or not app.get("Config", {}).get("Image", "").startswith("frappe/"):
            continue
        sites[project] = _build_site(project, items, app, tstatus)

    for d in sorted(config.SITES_DIR.iterdir()) if config.SITES_DIR.exists() else []:
        if d.is_dir() and d.name not in sites and (d / "site.json").exists():
            sites[d.name] = _build_site(d.name, [], None, tstatus)
    return sorted(sites.values(), key=lambda s: s["domain"])


def _build_site(project: str, items: list[dict], app: dict | None, tstatus: dict) -> dict:
    meta = load_meta(project)
    labels = (app or {}).get("Config", {}).get("Labels") or {}
    compose_files = labels.get("com.docker.compose.project.config_files", "")
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    compose_file = compose_files.split(",")[0] if compose_files else meta.get("compose_file", "")
    env = read_env_file(Path(workdir) / ".env") if workdir else {}
    env = {**env, **read_env_file(site_dir(project) / ".env")} if meta else env
    cenv = _container_env(app) if app else {}
    domain = meta.get("domain") or cenv.get("SITES") or env.get("SITES") or project.replace("_", ".")
    version = env.get("ERPNEXT_VERSION") or (app or {}).get("Config", {}).get("Image", ":").split(":")[-1]
    summaries = sorted((docker.container_summary(c) for c in items),
                       key=lambda s: SERVICES.index(s["service"]) if s["service"] in SERVICES else 9)
    app_summary = next((s for s in summaries if s["name"] == f"{project}-app" or s["service"] == "app"), None)
    ssl = bool(meta.get("ssl")) or any(k.endswith(".tls.certresolver") for k in labels)
    http_port = tstatus.get("http_port", 80)
    if not app_summary:
        state = "not deployed"
    elif app_summary["running"]:
        state = {"healthy": "running", "unhealthy": "unhealthy", "starting": "starting"}.get(
            app_summary["health"] or "", "running")
    else:
        state = "stopped"
    return {
        "project": project,
        "domain": domain,
        "version": version,
        "state": state,
        "containers": summaries,
        "app_container": app_summary["name"] if app_summary else f"{project}-app",
        "db_container": f"{project}-db",
        "compose_file": compose_file,
        "workdir": workdir or str(site_dir(project)),
        "managed": bool(meta),
        "mode": meta.get("mode", "local" if domain.endswith((".localhost", ".local", ".test")) else "server"),
        "ssl": ssl,
        "dev_mount": meta.get("dev_mount", False),
        "apps_dir": meta.get("apps_dir", ""),
        "url": traefik.site_url(domain, ssl, http_port),
        "public_url": f"https://{domain}" if domain in tunnel.public_hostnames() else "",
        "created": meta.get("created", ""),
        "env": env,
    }


async def get(project: str) -> dict | None:
    if not PROJECT_RE.match(project):
        return None
    for s in await discover():
        if s["project"] == project:
            return s
    return None


def load_meta(project: str) -> dict:
    try:
        return json.loads((site_dir(project) / "site.json").read_text())
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- creation

def validate_new(form: dict) -> dict:
    domain = (form.get("domain") or "").strip().lower()
    if not DOMAIN_RE.match(domain):
        raise ValueError("Enter a valid site name, e.g. mysite.localhost or erp.example.com")
    project = safe_name(domain)
    version = (form.get("version") or "").strip()
    if not versions.is_valid(version):
        raise ValueError("Pick a valid ERPNext version tag (e.g. v15.80.1)")
    mode = form.get("mode") or "local"
    if mode not in ("local", "server"):
        raise ValueError("Unknown environment")
    admin_password = form.get("admin_password") or "admin"
    if len(admin_password) < 4 or any(c in admin_password for c in "\"'`$\\\n "):
        raise ValueError("Admin password: at least 4 characters, no quotes, spaces, $ or backslashes")
    apps = ["erpnext"]
    for extra in (form.get("extra_apps") or "").replace(",", " ").split():
        if extra and APP_RE.match(extra) and extra not in apps:
            apps.append(extra)
    web_server = form.get("web_server") or ("production" if mode == "server" else "development")
    if web_server not in ("production", "development"):
        raise ValueError("Unknown web server")
    mem = form.get("app_memory") or "2G"
    if not re.fullmatch(r"\d{1,3}[MG]", mem):
        raise ValueError("Memory limit must look like 2G or 1536M")
    cpus = form.get("app_cpus") or "1.0"
    if not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", str(cpus)):
        raise ValueError("CPU limit must be a number like 1.0")
    return {
        "domain": domain,
        "project": project,
        "version": version,
        "mode": mode,
        "ssl": bool(form.get("ssl")) and mode == "server",
        "dev_mount": bool(form.get("dev_mount")) and mode == "local",
        "developer_mode": bool(form.get("developer_mode")),
        "admin_password": admin_password,
        "install_apps": apps,
        "web_server": web_server,
        "app_memory": mem,
        "app_cpus": str(cpus),
    }


def render_site_files(opts: dict, apps_dir: str) -> dict[str, str]:
    env = traefik.yaml_env
    m = re.search(r":(\d+)", config.MARIADB_IMAGE)
    production = opts.get("web_server", "development") == "production"
    ctx = dict(opts, apps_dir=apps_dir, mariadb_image=config.MARIADB_IMAGE, redis_image=config.REDIS_IMAGE,
               mariadb_major=int(m.group(1)) if m else 11, web_port=8080 if production else 8000)
    web_cmd = ("/home/frappe/frappe-bench/env/bin/gunicorn --chdir=/home/frappe/frappe-bench/sites "
               "--bind=0.0.0.0:8000 --threads=4 --workers=2 --worker-class=gthread "
               "--worker-tmp-dir=/dev/shm --timeout=120 --preload frappe.app:application"
               if production else "bench serve --port 8000")
    programs = [
        {"name": "frappe-web", "command": web_cmd},
        {"name": "frappe-schedule", "command": "bench schedule"},
        {"name": "frappe-worker-short", "command": "bench worker --queue short"},
        {"name": "frappe-worker-long", "command": "bench worker --queue long"},
        {"name": "frappe-worker-default", "command": "bench worker --queue default"},
        {"name": "frappe-websocket", "command": "node /home/frappe/frappe-bench/apps/frappe/socketio.js"},
    ]
    if production:
        # the image's own nginx: serves /assets and /files, proxies to gunicorn + socket.io
        programs.append({"name": "frappe-nginx", "command": "/usr/local/bin/nginx-entrypoint.sh"})
    return {
        "docker-compose.yml": env.get_template("site.yml.j2").render(**ctx),
        "supervisor/supervisord.conf": env.get_template("supervisord.conf.j2").render(),
        "supervisor/frappe.conf": env.get_template("frappe.conf.j2").render(domain=opts["domain"],
                                                                            programs=programs),
    }


async def compose(project: str, *args: str, on_line=None, timeout: float | None = 600):
    d = site_dir(project)
    s = None
    compose_file = d / "docker-compose.yml"
    workdir = d
    if not compose_file.exists():
        s = await get(project)
        if not s or not s["compose_file"] or not Path(s["compose_file"]).exists():
            raise JobError("No compose file found for this site - it can only be controlled container by container")
        compose_file, workdir = Path(s["compose_file"]), Path(s["workdir"])
    cmd = [*(await docker.compose_cmd()), "-p", project, "-f", str(compose_file),
           "--project-directory", str(workdir), *args]
    if on_line:
        return await docker.stream(cmd, on_line, cwd=str(workdir))
    return await docker.run(cmd, timeout=timeout)


async def create(job: Job, opts: dict) -> dict:
    domain, project, version = opts["domain"], opts["project"], opts["version"]
    image = f"{config.ERPNEXT_IMAGE}:{version}"

    await job.step("Checking Docker")
    info = await docker.info()
    if not info["ok"]:
        raise JobError(f"Docker is not available: {info['error']}")
    await job.ok(f"Docker {info['server_version']} · compose {info['compose_version']}")

    existing = [c for c in await docker.all_containers()
                if c.get("Name", "").lstrip("/").startswith(f"{project}-")]
    if existing:
        raise JobError(f"Containers for '{project}' already exist. Delete that site first or pick another name.")
    if (site_dir(project) / "docker-compose.yml").exists():
        raise JobError(f"A site folder already exists at {site_dir(project)}")

    await job.step("Checking the Traefik reverse proxy")
    await docker.ensure_network(config.TRAEFIK_NETWORK)
    tst = await traefik.status()
    if not tst["running"]:
        if tst["exists"]:
            await job.log("Traefik exists but is stopped - starting it")
            await traefik.control("start")
            tst = await traefik.status()
        if not tst["running"]:
            cfg = traefik.load_config()
            if not traefik.CONFIG_FILE.exists():
                cfg["mode"] = opts["mode"]
                if opts["mode"] == "server":
                    cfg["http_port"] = 80
                else:
                    cfg["http_port"] = traefik.suggest_port(8081, 8099, {cfg["dashboard_port"]}) or 8081
            if opts["ssl"] and not cfg["ssl"]:
                raise JobError("SSL requested but Traefik isn't configured for HTTPS. Set it up on the Traefik page first.")
            await job.log(f"Auto-installing Traefik ({cfg['mode']} mode, HTTP port {cfg['http_port']})")
            await traefik.setup(job, {**cfg, "_cf_token": ""})
            tst = await traefik.status()
    else:
        await job.ok(f"Traefik is running (HTTP port {tst['http_port']})")
    if opts["ssl"] and not (tst.get("live_https") or tst["config"].get("ssl")):
        raise JobError("SSL requested but the running Traefik has no HTTPS entrypoint. Reconfigure it with SSL first.")

    await job.step(f"Pulling images ({image}, {config.MARIADB_IMAGE}, {config.REDIS_IMAGE})")
    for img in (image, config.MARIADB_IMAGE, config.REDIS_IMAGE):
        if (await docker.docker("image", "inspect", img, timeout=20)).ok:
            await job.log(f"{img}: already present")
            continue
        last = {"t": 0.0}

        async def progress(line: str, last=last) -> None:
            # throttle the very chatty pull progress output
            if "Downloading" in line or "Extracting" in line:
                if time.time() - last["t"] < 2:
                    return
                last["t"] = time.time()
            await job.log(line)

        if await docker.stream([docker.docker_bin(), "pull", img], progress) != 0:
            raise JobError(f"Could not pull {img}. Check the version tag and your internet connection.")
    await job.ok("Images ready")

    d = site_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    apps_dir = ""
    if opts["dev_mount"]:
        apps_path = config.DEV_APPS_ROOT / f"{project}-frappe-bench" / "apps"
        apps_dir = str(apps_path)
        await job.step(f"Preparing apps folder for VS Code: {apps_dir}")
        apps_path.mkdir(parents=True, exist_ok=True)
        if not (apps_path / "frappe").exists():
            uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
            # Container must run as uid 1000 (frappe); on Linux keep host ownership compatible.
            code = await docker.stream([docker.docker_bin(), "run", "--rm", "--user", "root",
                                        "-v", f"{apps_dir}:/apps", image, "bash", "-c",
                                        "cp -a /home/frappe/frappe-bench/apps/. /apps/ && chown -R 1000:1000 /apps"],
                                       job.log)
            if code != 0:
                raise JobError("Copying apps out of the image failed")
            await job.ok(f"Copied frappe & erpnext sources (owner 1000:1000; host user {uid})")
        else:
            await job.ok("Apps folder already populated - reusing it")

    await job.step("Writing configuration")
    db_pw, redis_pw = gen_password(), gen_password()
    env_text = (f"ERPNEXT_VERSION={version}\nDB_PASSWORD={db_pw}\nREDIS_PASSWORD={redis_pw}\n"
                f"ADMIN_PASSWORD={opts['admin_password']}\nFRAPPE_SITE_NAME_HEADER={domain}\nSITES={domain}\n")
    (d / ".env").write_text(env_text)
    (d / ".env").chmod(0o600)
    for rel, text in render_site_files(opts, apps_dir).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    meta = {k: opts[k] for k in ("domain", "version", "mode", "ssl", "dev_mount", "developer_mode",
                                 "install_apps", "web_server", "app_memory", "app_cpus")}
    meta.update(apps_dir=apps_dir, compose_file=str(d / "docker-compose.yml"),
                created=time.strftime("%Y-%m-%d %H:%M"))
    (d / "site.json").write_text(json.dumps(meta, indent=2))
    await job.ok(f"Wrote {d}/docker-compose.yml, .env and supervisor config")

    r = await compose(project, "config", "--quiet")
    if not r.ok:
        raise JobError(f"Generated compose file is invalid: {r.text}")

    await job.step("Starting database & redis, then creating the site (3-6 minutes)")
    code = await compose(project, "up", "-d", "create-site", on_line=job.log)
    if code != 0:
        raise JobError("docker compose could not start the database / create-site containers")
    name = f"{project}-create-site"
    await job.log(f"── output of {name} ──", "debug")
    seen_bars: set[tuple[str, int]] = set()

    async def site_line(line: str) -> None:
        # bench redraws "Updating DocTypes for x : [====  ] 42%" for every percent; keep 25% steps only
        m = PROGRESS_RE.search(line)
        if m:
            step = (m.group(1).strip(), int(m.group(2)) // 25 * 25)
            if step in seen_bars:
                return
            seen_bars.add(step)
        await job.log("  " + line)

    follow = asyncio.create_task(docker.stream([docker.docker_bin(), "logs", "-f", name], site_line))
    try:
        # `docker wait` blocks until the container exits and prints its exit code
        rc = await docker.docker("wait", name, timeout=1800)
        await asyncio.sleep(1)  # let the last log lines arrive
    finally:
        follow.cancel()
    if rc.out.strip() != "0":
        tail = await docker.docker("logs", "--tail", "4", name)
        hint = ""
        if "SSL is required" in tail.text:
            hint = " (the ERPNext image's MariaDB client needs a TLS-capable server - use mariadb:11.8)"
        raise JobError(f"Site creation failed (exit {rc.out.strip() or rc.text}){hint} - see the output above")
    await job.ok(f"Site {domain} created")

    await job.step("Starting the application container")
    if await compose(project, "up", "-d", on_line=job.log) != 0:
        raise JobError("Could not start the app container")

    await job.step("Waiting for Frappe to answer /api/method/ping")
    app = f"{project}-app"
    deadline = time.time() + 600
    ready = False
    last_log = 0.0
    while time.time() < deadline:
        r = await docker.docker("exec", app, "curl", "-fsS", "-H", f"Host: {domain}",
                                "http://localhost:8000/api/method/ping", timeout=20)
        if r.ok and "pong" in r.out:
            ready = True
            break
        if time.time() - last_log > 20:
            tail = await docker.docker("logs", "--tail", "3", app, timeout=20)
            await job.log("… still starting: " + (tail.text.splitlines() or ["(no output yet)"])[-1])
            last_log = time.time()
        await asyncio.sleep(5)
    if not ready:
        raise JobError(f"App did not become ready in 10 minutes. Check logs of {app}.")
    await job.ok("Frappe is up inside the container")

    tst = await traefik.status()
    url = traefik.site_url(domain, opts["ssl"], tst["http_port"])
    await job.step("Checking routing through Traefik")
    ok = await asyncio.to_thread(_probe_traefik, domain, tst["http_port"])
    if ok:
        await job.ok(f"Traefik routes {domain} correctly")
    else:
        await job.warn("Traefik did not answer for this host yet - it can take a few seconds to pick up labels")

    hosts = hosts_status(domain)
    if hosts["needed"] and not hosts["present"]:
        await job.warn(f"Add {domain} to /etc/hosts to open it in the browser (see the site page).")

    await job.log("")
    await job.ok(f"🌐 {url}")
    await job.ok(f"👤 Administrator  🔑 {opts['admin_password']}")
    await job.log(f"💾 MariaDB root password and Redis password are stored in {d / '.env'}")
    if apps_dir:
        await job.log(f"💻 Open in VS Code: code {apps_dir}")
    return {"url": url, "project": project}


def _probe_traefik(domain: str, port: int) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/method/ping", headers={"Host": domain})
    for _ in range(6):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - local probe
                if resp.status in (200, 301, 302, 308):
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 308):
                return True
        except OSError:
            pass
        time.sleep(3)
    return False


# --------------------------------------------------------------------------- hosts file

def hosts_status(domain: str) -> dict:
    needed = not domain.endswith(".localhost")
    present = False
    try:
        for line in Path("/etc/hosts").read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and domain in parts[1:]:
                present = True
    except OSError:
        pass
    return {
        "needed": needed,
        "present": present,
        "writable": os.access("/etc/hosts", os.W_OK),
        "command": f"echo '127.0.0.1 {domain}' | sudo tee -a /etc/hosts",
        "remove_command": (f"sudo sed -i '' '/[[:space:]]{re.escape(domain)}$/d' /etc/hosts"
                           if os.uname().sysname == "Darwin"
                           else f"sudo sed -i '/[[:space:]]{re.escape(domain)}$/d' /etc/hosts"),
    }


def hosts_add(domain: str) -> None:
    if not DOMAIN_RE.match(domain):
        raise ValueError("bad domain")
    st = hosts_status(domain)
    if st["present"]:
        return
    if not st["writable"]:
        raise PermissionError(f"/etc/hosts is not writable by the UI. Run: {st['command']}")
    with open("/etc/hosts", "a") as fh:
        fh.write(f"\n127.0.0.1 {domain}\n")


# --------------------------------------------------------------------------- operations

async def container_action(site: dict, action: str, job: Job) -> dict:
    project = site["project"]
    has_compose = bool(site["compose_file"]) and Path(site["compose_file"]).exists()
    if action == "start":
        if has_compose:
            if await compose(project, "up", "-d", on_line=job.log) != 0:
                raise JobError("compose up failed")
        else:
            for c in site["containers"]:
                if c["service"] != "create-site":
                    await job.log((await docker.docker("start", c["name"])).text)
    elif action == "stop":
        names = [c["name"] for c in site["containers"] if c["running"]]
        if names:
            await docker.stream([docker.docker_bin(), "stop", *names], job.log)
    elif action == "restart":
        names = [c["name"] for c in site["containers"] if c["service"] != "create-site"]
        await docker.stream([docker.docker_bin(), "restart", *names], job.log)
    elif action == "recreate":
        if not has_compose:
            raise JobError("No compose file - cannot recreate")
        if await compose(project, "up", "-d", "--force-recreate", "--no-deps", "app", on_line=job.log) != 0:
            raise JobError("Recreate failed")
    elif action == "down":
        if await compose(project, "down", on_line=job.log) != 0:
            raise JobError("compose down failed")
    else:
        raise JobError(f"Unknown action {action}")
    return {}


async def delete(site: dict, job: Job, remove_dev_dir: bool) -> dict:
    project = site["project"]
    await job.step("Stopping and removing containers, networks and volumes")
    has_compose = bool(site["compose_file"]) and Path(site["compose_file"]).exists()
    if has_compose:
        await compose(project, "down", "-v", "--remove-orphans", on_line=job.log)
    names = [c["name"] for c in site["containers"]]
    if names:
        await docker.stream([docker.docker_bin(), "rm", "-f", *names], job.log)
    vols = await docker.docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}")
    for v in vols.out.split():
        await job.log((await docker.docker("volume", "rm", v)).text)
    nets = await docker.docker("network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}")
    for n in nets.out.split():
        await docker.docker("network", "rm", n)
    d = site_dir(project)
    if d.exists() and d.resolve().is_relative_to(config.SITES_DIR.resolve()):
        shutil.rmtree(d)
        await job.ok(f"Removed {d}")
    if remove_dev_dir:
        dev = config.DEV_APPS_ROOT / f"{project}-frappe-bench"
        if dev.exists() and dev.resolve().is_relative_to(config.DEV_APPS_ROOT.resolve()):
            # files are owned by uid 1000 inside docker; remove through a container to avoid permission issues
            await docker.docker("run", "--rm", "-v", f"{dev}:/x", config.REDIS_IMAGE, "sh", "-c",
                                "rm -rf /x/* /x/.[!.]*", timeout=300)
            shutil.rmtree(dev, ignore_errors=True)
            await job.ok(f"Removed {dev}")
    hs = hosts_status(site["domain"])
    if hs["present"]:
        await job.warn(f"/etc/hosts still contains {site['domain']}. Remove with: {hs['remove_command']}")
    return {"redirect": "/"}


async def supervisor_status(site: dict) -> dict:
    r = await docker.docker("exec", site["app_container"], config.SUPERVISORCTL, "-c",
                            config.SUPERVISOR_CONF, "status", timeout=30)
    procs = []
    for line in r.out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            procs.append({"name": parts[0], "state": parts[1], "info": parts[2] if len(parts) > 2 else ""})
    return {"ok": bool(procs), "processes": procs, "error": "" if procs else (r.text or "Supervisor not reachable")}


async def supervisor_action(site: dict, action: str, process: str) -> docker.Result:
    if action not in ("start", "stop", "restart") or not PROCESS_RE.match(process):
        raise ValueError("Invalid process action")
    if process != "all" and ":" not in process:
        process = f"frappe:{process}"
    return await docker.docker("exec", site["app_container"], config.SUPERVISORCTL, "-c",
                               config.SUPERVISOR_CONF, action, process, timeout=120)


LOG_SOURCES = {
    "web": ("file", "frappe-web"),
    "worker-default": ("file", "frappe-worker-default"),
    "worker-short": ("file", "frappe-worker-short"),
    "worker-long": ("file", "frappe-worker-long"),
    "schedule": ("file", "frappe-schedule"),
    "websocket": ("file", "frappe-websocket"),
    "supervisord": ("file", "supervisord"),
    "bench-frappe": ("bench", "frappe.log"),
    "bench-web-error": ("bench", "web.error.log"),
    "app": ("docker", "app"),
    "create-site": ("docker", "create-site"),
    "db": ("docker", "db"),
    "redis": ("docker", "redis"),
}


def log_command(site: dict, source: str, follow: bool, lines: int = 300) -> list[str]:
    if source not in LOG_SOURCES:
        raise ValueError("Unknown log source")
    kind, target = LOG_SOURCES[source]
    lines = max(10, min(int(lines), 5000))
    d = docker.docker_bin()
    if kind == "docker":
        cmd = [d, "logs", "--tail", str(lines)]
        if follow:
            cmd.append("-f")
        return [*cmd, f"{site['project']}-{target}"]
    path = (f"/home/frappe/supervisor/logs/{target}.log" if kind == "file"
            else f"{config.BENCH_DIR}/logs/{target}")
    return [d, "exec", site["app_container"], "tail", "-n", str(lines), *(["-F"] if follow else []), path]


BENCH_COMMANDS = {
    "list-apps": "List installed apps",
    "migrate": "Run migrations (bench migrate)",
    "clear-cache": "Clear cache",
    "clear-website-cache": "Clear website cache",
    "build": "Build assets (bench build)",
    "backup": "Backup database + files",
    "enable-scheduler": "Enable scheduler",
    "disable-scheduler": "Disable scheduler",
    "developer-mode-on": "Enable developer mode",
    "developer-mode-off": "Disable developer mode",
    "doctor": "Scheduler / worker diagnostics",
}


async def bench(site: dict, job: Job, command: str, params: dict) -> dict:
    app, domain = site["app_container"], site["domain"]
    d = docker.docker_bin()
    base = [d, "exec", "-w", config.BENCH_DIR, app, "bench"]
    on_site = [*base, "--site", domain]
    restart_after = False
    if command == "list-apps":
        cmd = [*on_site, "list-apps"]
    elif command == "migrate":
        cmd, restart_after = [*on_site, "migrate"], True
    elif command == "clear-cache":
        cmd = [*on_site, "clear-cache"]
    elif command == "clear-website-cache":
        cmd = [*on_site, "clear-website-cache"]
    elif command == "build":
        cmd, restart_after = [*base, "build"], True
    elif command == "backup":
        cmd = [*on_site, "backup", "--with-files"]
    elif command in ("enable-scheduler", "disable-scheduler"):
        cmd = [*on_site, command]
    elif command in ("developer-mode-on", "developer-mode-off"):
        cmd = [*on_site, "set-config", "developer_mode", "1" if command.endswith("on") else "0"]
        restart_after = True
    elif command == "doctor":
        cmd = [*base, "doctor"]
    elif command == "set-admin-password":
        pw = params.get("password", "")
        if len(pw) < 4 or any(c in pw for c in "\n\"'`$\\"):
            raise JobError("Password: at least 4 characters without quotes, $ or backslashes")
        cmd = [*on_site, "set-admin-password", pw]
    elif command == "get-app":
        src = (params.get("source") or "").strip()
        branch = (params.get("branch") or "").strip()
        if not (APP_RE.match(src) or GIT_URL_RE.match(src)):
            raise JobError("App must be an app name (e.g. hrms) or a git URL (https://… or git@…)")
        if branch and not BRANCH_RE.match(branch):
            raise JobError("Invalid branch name")
        cmd = [*base, "get-app", *(["--branch", branch] if branch else []), src]
        restart_after = True
    elif command in ("install-app", "uninstall-app"):
        name = (params.get("app") or "").strip()
        if not APP_RE.match(name):
            raise JobError("Invalid app name")
        cmd = [*on_site, command, name] + (["--yes", "--no-backup"] if command == "uninstall-app" else [])
        restart_after = True
    elif command == "console":
        raise JobError("Use the Terminal tab for an interactive console")
    else:
        raise JobError(f"Unknown bench command {command}")

    await job.step("$ " + " ".join(cmd[cmd.index("bench"):]))
    code = await docker.stream(cmd, job.log)
    if code != 0:
        if command == "install-app":
            await job.warn("If this failed with 'Access denied', run 'Fix DB permissions' and try again.")
        raise JobError(f"bench {command} exited with code {code}")
    if command == "get-app":
        await docker.docker("exec", "-w", config.BENCH_DIR, app, "bash", "-c", "ls -1 apps > sites/apps.txt")
    if restart_after:
        await job.step("Restarting Frappe processes")
        r = await supervisor_action(site, "restart", "all")
        await job.log(r.text)
    if command == "backup":
        await job.log("Backups are listed in the 'Backups' card - download them from there.")
    return {}


async def list_apps(site: dict) -> list[dict]:
    """Apps in the bench, flagged with whether they are installed on this site."""
    app = site["app_container"]
    bench_r, site_r = await asyncio.gather(
        docker.docker("exec", "-w", config.BENCH_DIR, app, "sh", "-c",
                      'for d in apps/*/; do basename "$d"; done', timeout=30),
        docker.docker("exec", "-w", config.BENCH_DIR, app, "bench", "--site", site["domain"], "list-apps",
                      timeout=60),
    )
    if not bench_r.ok:
        raise RuntimeError(bench_r.text or "Could not list the bench apps folder")
    bench_apps = [a for a in bench_r.out.split() if APP_RE.match(a)]
    installed: dict[str, dict] = {}
    for line in site_r.out.splitlines():
        parts = line.split()
        if parts and parts[0] in bench_apps:
            installed[parts[0]] = {"version": parts[1] if len(parts) > 1 else "",
                                   "branch": parts[2] if len(parts) > 2 else ""}
    if not site_r.ok and not installed:
        raise RuntimeError(site_r.text or "bench list-apps failed")
    order = list(installed) + sorted(a for a in bench_apps if a not in installed)
    return [{"name": a, "installed": a in installed, **installed.get(a, {"version": "", "branch": ""})}
            for a in order]


async def list_backups(site: dict) -> list[dict]:
    path = f"{config.BENCH_DIR}/sites/{site['domain']}/private/backups"
    r = await docker.docker("exec", site["app_container"], "sh", "-c",
                            'cd "$1" 2>/dev/null && ls -1t | head -60 | while read f; do '
                            'printf "%s\\t%s\\n" "$f" "$(du -h "$f" | cut -f1)"; done', "sh", path, timeout=30)
    out = []
    for line in r.out.splitlines():
        if "\t" in line:
            name, size = line.split("\t", 1)
            out.append({"name": name, "size": size, "path": f"{path}/{name}"})
    return out


async def fix_db_permissions(site: dict, job: Job) -> dict:
    """Port of fix-db-permissions.sh: grant the site's DB user access from any host."""
    await job.step("Reading site_config.json")
    r = await docker.docker("exec", site["app_container"], "cat",
                            f"{config.BENCH_DIR}/sites/{site['domain']}/site_config.json")
    if not r.ok:
        raise JobError(f"Cannot read site_config.json: {r.text}")
    cfg = json.loads(r.out)
    db_name, db_pw = cfg.get("db_name"), cfg.get("db_password")
    if not db_name or not db_pw or not re.fullmatch(r"[\w]+", db_name):
        raise JobError("site_config.json has no usable db_name / db_password")
    root_pw = site["env"].get("DB_PASSWORD")
    if not root_pw:
        db = await docker.inspect([site["db_container"]])
        root_pw = _container_env(db[0]).get("MARIADB_ROOT_PASSWORD") if db else None
    if not root_pw:
        raise JobError("MariaDB root password not found (.env or db container env)")
    esc_pw = db_pw.replace("\\", "\\\\").replace("'", "\\'")
    sql = (f"CREATE USER IF NOT EXISTS '{db_name}'@'%' IDENTIFIED BY '{esc_pw}';\n"
           f"ALTER USER '{db_name}'@'%' IDENTIFIED BY '{esc_pw}';\n"
           f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_name}'@'%';\n"
           f"CREATE USER IF NOT EXISTS '{db_name}'@'localhost' IDENTIFIED BY '{esc_pw}';\n"
           f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_name}'@'localhost';\nFLUSH PRIVILEGES;\n")
    await job.step(f"Granting '{db_name}'@'%' full access to `{db_name}`")
    res = await docker.docker("exec", "-i", "-e", f"MYSQL_PWD={root_pw}", site["db_container"], "sh", "-c",
                              "if command -v mariadb >/dev/null; then exec mariadb -uroot; else exec mysql -uroot; fi",
                              stdin=sql.encode())
    if not res.ok:
        raise JobError(f"MariaDB rejected the grants: {res.text}")
    await job.ok("Database permissions fixed - app installation should work now")
    return {}


async def fix_restart_policy(site: dict, job: Job) -> dict:
    for c in site["containers"]:
        if c["service"] == "create-site" or c["name"].endswith("-create-site"):
            continue
        r = await docker.docker("update", "--restart=unless-stopped", c["name"])
        await (job.ok if r.ok else job.warn)(f"{c['name']}: {'restart=unless-stopped' if r.ok else r.text}")
    return {}


async def install_packages(site: dict, job: Job, container: str, packages: list[str]) -> dict:
    allowed = {c["name"] for c in site["containers"]}
    if container not in allowed:
        raise JobError("Unknown container")
    bad = [p for p in packages if not PKG_RE.match(p)]
    if bad or not packages:
        raise JobError(f"Invalid package names: {', '.join(bad) or '(none)'}")
    image = next(c["image"] for c in site["containers"] if c["name"] == container)
    alpine = "alpine" in image
    script = ("apk add --no-cache " if alpine else
              "export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y --no-install-recommends ")
    cmd = [docker.docker_bin(), "exec", "--user", "root", container, "sh", "-c", script + '"$@"', "sh", *packages]
    await job.step(f"Installing {' '.join(packages)} in {container}")
    if await docker.stream(cmd, job.log) != 0:
        raise JobError("Package installation failed")
    await job.warn("Packages live in the container layer - they are lost when the container is re-created.")
    return {}
